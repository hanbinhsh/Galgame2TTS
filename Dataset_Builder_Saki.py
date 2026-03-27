import os
import re
import glob
import soundfile as sf
import sys
import subprocess
import platform
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QTableWidget, QTableWidgetItem, 
                               QHeaderView, QCheckBox, QMessageBox, QLabel, QComboBox)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtGui import QColor

# === 消除 FFmpeg 底层警告 (可选但推荐) ===
os.environ["AV_LOG_LEVEL"] = "quiet"

# === 配置区 ===
EVT_FILES_DIR = r"./saki_voice/evt/"         # 存放所有 .evt 文件的文件夹
OGG_DIR = r"./saki_voice/voice/"             # 存放所有 .ogg 音频的文件夹
OUTPUT_LIST = r"./GPT_SoVITS_saki.list"           # 最终生成的训练列表路径

# 支持提取多个角色名或同一角色的不同写法（包含“サキの声”等变体）
TARGET_CHARACTERS = ["サキ", "咲"]       
CHARACTER_NAME_IN_LIST = "Saki"              # 在 GPT-SoVITS 中给角色起的英文名

# === 数据处理后端 ===
def check_text_validity(text):
    if re.fullmatch(r'…+', text.strip()): return False, "纯省略号"
    if re.fullmatch(r'あ+', text.strip()): return False, "纯啊啊啊"
    if len(re.findall(r'[ぁぃぅぇぉゃゅょ]', text)) >= 3: return False, "过多小写假名(疑似喘息)"
    if len(re.findall(r'ー', text)) >= 3: return False, "过多长音"
    if len(re.findall(r'ん', text)) >= 3: return False, "过多'嗯'"
    if re.search(r'[a-zA-Z]', text): return False, "包含英文字母"
    return True, "文本正常"

def clean_text(text):
    # --- 新增：处理游戏特有的格式标签 ---
    # 1. 处理 Ruby 注音标签 (例如: <RB='ひっぷげろう'>匹夫下郎<RB>)
    text = re.sub(r"<RB='[^']*'>", '', text)  # 删掉前置注音 <RB='xxx'>
    text = re.sub(r"<RB=[^>]+>", '', text)    # 兼容没有单引号的异常情况 <RB=xxx>
    text = re.sub(r"<RB>", '', text)          # 删掉后置闭合标签 <RB>
    
    # 2. 处理关键字高亮标签 (例如: <K_174>矛盾<_K>)
    text = re.sub(r"<K_\d+>", '', text)       # 删掉前置高亮 <K_123>
    text = re.sub(r"<_K>", '', text)          # 删掉后置闭合标签 <_K>
    
    # --- 原有的常规清理 ---
    text = re.sub(r'[\s　\\]', '', text)
    text = re.sub(r'\[.+?\]', '', text)
    text = re.sub(r'[「」\(\)（）♪●『』]', '', text) 
    text = re.sub(r'～|\n|。\\n', '。', text)
    return text

def read_evt_file(filepath):
    # Galgame 脚本通常是 shift_jis 编码
    encodings = ['shift_jis', 'utf-8', 'cp932', 'utf-16', 'utf-16le']
    for enc in encodings:
        try:
            with open(filepath, 'r', encoding=enc) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    return ""

# === 自定义单元格排序逻辑 ===
class NumericItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except ValueError:
            return self.text() < other.text()

class CheckBoxSortItem(QTableWidgetItem):
    def __lt__(self, other):
        table = self.tableWidget()
        if not table: return False
        row_self = table.row(self)
        row_other = table.row(other)
        w_self = table.cellWidget(row_self, 0)
        w_other = table.cellWidget(row_other, 0)
        
        if w_self and w_other:
            cb_self = w_self.layout().itemAt(0).widget().isChecked()
            cb_other = w_other.layout().itemAt(0).widget().isChecked()
            return cb_self < cb_other
        return False

def parse_dataset():
    dataset = []
    evt_files = glob.glob(os.path.join(EVT_FILES_DIR, "*.evt"))

    for evt_file in evt_files:
        content = read_evt_file(evt_file)
        if not content: 
            continue
            
        lines = content.splitlines()
        i = 0
        
        while i < len(lines):
            line = lines[i].strip()

            name_match = re.search(r'@\[DrawName\s+name\s*=\s*"([^"]+)"\]', line)
            
            if name_match:
                char_name = name_match.group(1)
                
                if any(target in char_name for target in TARGET_CHARACTERS):
                    i += 1
                    audio_filename = None
                    dialogue_raw = ""

                    while i < len(lines):
                        sub_line = lines[i].strip()
                        
                        if "@[PageBreak]" in sub_line or "@[DrawName" in sub_line:
                            break
                            
                        voice_match = re.search(r"<voice\s+[^>]*file='([^']+)'", sub_line)
                        if voice_match:
                            audio_filename = voice_match.group(1)
                        elif not sub_line.startswith(";") and not sub_line.startswith("@") and sub_line != "":
                            # --- 核心修复 1：阻断包含日文注释的代码行 ---
                            # 如果一行以英文字母（如 ScSetFlag）或星号（如 *lbl_）开头，它必定是代码，直接跳过
                            if re.match(r'^[A-Za-z*]', sub_line):
                                pass
                            else:
                                dialogue_raw += sub_line

                        i += 1

                    if audio_filename and dialogue_raw:
                        ogg_path = os.path.join(OGG_DIR, audio_filename)
                        
                        # --- 核心修复 2：无条件提前清洗文本 ---
                        # 彻底解决局部变量残留导致的“文本串号”问题
                        cleaned_text = clean_text(dialogue_raw)
                        
                        is_valid = True
                        reason = "正常"
                        duration = 0

                        if not os.path.exists(ogg_path):
                            is_valid = False
                            reason = "音频文件缺失"
                        else:
                            try:
                                data, samplerate = sf.read(ogg_path)
                                duration = len(data) / float(samplerate)
                            except:
                                duration = 0

                            if not (2.0 <= duration <= 15.0):
                                is_valid = False
                                reason = "时长越界"
                            else:
                                # 使用当前语音独有的 cleaned_text 进行合法性校验
                                text_valid, text_reason = check_text_validity(cleaned_text)
                                if not text_valid:
                                    is_valid = False
                                    reason = text_reason
                        
                        dataset.append({
                            'path': os.path.abspath(ogg_path),
                            'text': cleaned_text,
                            'duration': round(duration, 2),
                            'is_valid': is_valid,
                            'reason': reason
                        })
                    continue 
            i += 1
            
    return dataset


# === GUI 前端 ===
class DatasetEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Galgame 语音数据集审查工具")
        self.resize(1150, 600)
        self.dataset = parse_dataset()
        
        self.current_play_btn = None
        
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.audio_output.setVolume(0.8)
        self.player.setAudioOutput(self.audio_output)
        self.player.playbackStateChanged.connect(self.on_playback_state_changed)
        
        self.initUI()
        self.load_data_to_table()

    def initUI(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)

        # 顶部操作区
        btn_layout = QHBoxLayout()
        
        self.btn_select_all = QPushButton("全选 (当前视图)")
        self.btn_deselect_all = QPushButton("取消全选 (当前视图)")
        self.btn_reset_sort = QPushButton("↺ 恢复初始排序")
        self.btn_reset_sort.setStyleSheet("font-weight: bold; padding: 5px;")
        
        lbl_filter = QLabel("过滤筛选:")
        lbl_filter.setStyleSheet("font-weight: bold; margin-left: 15px;")
        self.combo_filter = QComboBox()
        self.combo_filter.addItem("全部状态/原因")
        self.combo_filter.currentTextChanged.connect(self.filter_table)
        
        self.lbl_count = QLabel("全局已选中: 0 / 总数: 0")
        self.lbl_count.setStyleSheet("font-size: 14px; font-weight: bold; margin-left: 20px; color: #333;")
        
        self.btn_export = QPushButton("导出勾选数据")
        self.btn_export.setStyleSheet("background-color: #4CAF50; color: white; font-weight: bold; padding: 5px 15px;")
        
        self.btn_select_all.clicked.connect(lambda: self.set_all_checkboxes(True))
        self.btn_deselect_all.clicked.connect(lambda: self.set_all_checkboxes(False))
        self.btn_reset_sort.clicked.connect(self.reset_original_sort)
        self.btn_export.clicked.connect(self.export_list)

        btn_layout.addWidget(self.btn_select_all)
        btn_layout.addWidget(self.btn_deselect_all)
        btn_layout.addWidget(self.btn_reset_sort)
        btn_layout.addWidget(lbl_filter)
        btn_layout.addWidget(self.combo_filter)
        btn_layout.addWidget(self.lbl_count)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_export)
        layout.addLayout(btn_layout)

        # 数据表格区
        self.table = QTableWidget()
        self.table.setColumnCount(8) 
        self.table.setHorizontalHeaderLabels(["选择 ↑↓", "试听", "源文件 (点击定位)", "角色名", "文本 (双击可编辑)", "时长(s) ↑↓", "状态/过滤原因 ↑↓", "OriginalIndex"])
        # 隐藏原始索引列
        self.table.setColumnHidden(7, True)
        
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch) 
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)
        
        layout.addWidget(self.table)

    def load_data_to_table(self):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.dataset))
        
        reasons_set = set()
        
        for row, data in enumerate(self.dataset):
            reasons_set.add(data['reason'])
            
            # 0. 选择列
            cb_sort_item = CheckBoxSortItem()
            self.table.setItem(row, 0, cb_sort_item)

            checkbox = QCheckBox()
            checkbox.setChecked(data['is_valid'])
            checkbox.stateChanged.connect(self.update_count_label)
            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.addWidget(checkbox)
            cb_layout.setAlignment(Qt.AlignCenter)
            cb_layout.setContentsMargins(0,0,0,0)
            self.table.setCellWidget(row, 0, cb_widget)

            # 1. 试听列
            play_btn = QPushButton("▶ 播放")
            play_btn.clicked.connect(lambda checked=False, p=data['path'], b=play_btn: self.play_audio(p, b))
            self.table.setCellWidget(row, 1, play_btn)

            # 2. 源文件列
            filename = os.path.basename(data['path'])
            btn_locate = QPushButton(f"{filename}")
            btn_locate.setStyleSheet("text-align: left; padding: 2px 10px; color: #555;")
            btn_locate.setToolTip("点击在资源管理器中定位该文件")
            btn_locate.clicked.connect(lambda checked=False, p=data['path']: self.locate_file(p))
            self.table.setCellWidget(row, 2, btn_locate)

            # 3. 角色名列
            char_item = QTableWidgetItem(CHARACTER_NAME_IN_LIST)
            char_item.setFlags(char_item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 3, char_item)

            # 4. 文本列
            text_item = QTableWidgetItem(data['text'])
            self.table.setItem(row, 4, text_item)

            # 5. 时长列
            dur_item = NumericItem(str(data['duration']))
            dur_item.setFlags(dur_item.flags() & ~Qt.ItemIsEditable)
            dur_item.setTextAlignment(Qt.AlignCenter) 
            self.table.setItem(row, 5, dur_item)

            # 6. 状态/原因列
            reason_item = QTableWidgetItem(data['reason'])
            reason_item.setFlags(reason_item.flags() & ~Qt.ItemIsEditable)
            if not data['is_valid']:
                reason_item.setForeground(QColor("red"))
            self.table.setItem(row, 6, reason_item)

            # 将文件绝对路径保存在角色名单元格中
            self.table.item(row, 3).setData(Qt.UserRole, data['path'])
            
            # 7. 隐藏序号列
            orig_idx_item = NumericItem(str(row))
            self.table.setItem(row, 7, orig_idx_item)
            
        for reason in sorted(reasons_set):
            self.combo_filter.addItem(reason)

        self.reset_original_sort()
        self.table.setSortingEnabled(True)
        self.update_count_label()

    def locate_file(self, file_path):
        sys_name = platform.system()
        try:
            if sys_name == "Windows":
                path = os.path.normpath(file_path)
                subprocess.Popen(f'explorer /select,"{path}"')
            elif sys_name == "Darwin":
                subprocess.Popen(['open', '-R', file_path])
            else:
                subprocess.Popen(['xdg-open', os.path.dirname(file_path)])
        except Exception as e:
            QMessageBox.warning(self, "定位失败", f"无法打开文件夹：\n{str(e)}")

    def reset_original_sort(self):
        self.table.sortByColumn(7, Qt.AscendingOrder)

    def filter_table(self, filter_text):
        for row in range(self.table.rowCount()):
            reason_item = self.table.item(row, 6)
            if filter_text == "全部状态/原因" or reason_item.text() == filter_text:
                self.table.setRowHidden(row, False)
            else:
                self.table.setRowHidden(row, True)

    def update_count_label(self):
        total = self.table.rowCount()
        selected = 0
        for row in range(total):
            cb_widget = self.table.cellWidget(row, 0)
            if cb_widget:
                checkbox = cb_widget.layout().itemAt(0).widget()
                if checkbox.isChecked():
                    selected += 1
        self.lbl_count.setText(f"全局已选中: {selected} / 总数: {total}")

    def play_audio(self, path, btn):
        if self.current_play_btn == btn and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.stop()
            return

        if self.current_play_btn:
            self.reset_button_style(self.current_play_btn)

        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(path))
        self.player.play()

        self.current_play_btn = btn
        btn.setText("⏹ 停止")
        btn.setStyleSheet("background-color: #e0f7fa; color: #00838f; font-weight: bold; border: 1px solid #00838f;")

    def on_playback_state_changed(self, state):
        if state == QMediaPlayer.PlaybackState.StoppedState:
            if self.current_play_btn:
                self.reset_button_style(self.current_play_btn)
                self.current_play_btn = None

    def reset_button_style(self, btn):
        btn.setText("▶ 播放")
        btn.setStyleSheet("")

    def set_all_checkboxes(self, state):
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                cb_widget = self.table.cellWidget(row, 0)
                checkbox = cb_widget.layout().itemAt(0).widget()
                checkbox.blockSignals(True)
                checkbox.setChecked(state)
                checkbox.blockSignals(False)
        self.update_count_label()

    def export_list(self):
        export_lines = []
        for row in range(self.table.rowCount()):
            cb_widget = self.table.cellWidget(row, 0)
            checkbox = cb_widget.layout().itemAt(0).widget()
            
            if checkbox.isChecked():
                path = self.table.item(row, 3).data(Qt.UserRole)
                character = self.table.item(row, 3).text()
                text = self.table.item(row, 4).text() 
                line = f"{path}|{character}|JA|{text}\n"
                export_lines.append(line)

        if not export_lines:
            QMessageBox.warning(self, "导出失败", "没有勾选任何数据！")
            return

        try:
            with open(OUTPUT_LIST, 'w', encoding='utf-8') as f:
                f.writelines(export_lines)
            QMessageBox.information(self, "导出成功", f"成功导出 {len(export_lines)} 条数据至：\n{OUTPUT_LIST}")
        except Exception as e:
            QMessageBox.critical(self, "导出错误", f"发生错误：{str(e)}")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    editor = DatasetEditor()
    editor.show()
    sys.exit(app.exec())