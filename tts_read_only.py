import time
import requests
import pyaudio
import threading
import queue
import concurrent.futures
import re

# ==========================================
# ⚙️ 参数配置区 
# ==========================================
TTS_API_URL = "http://127.0.0.1:9880" 
TARGET_LANG = "ja"                        
GPT_MODEL_PATH = r"D:\GPT-SoVITS-v2pro-20250604\GPT_weights_v2ProPlus\Kirie-e20.ckpt"
SOVITS_MODEL_PATH = r"D:\GPT-SoVITS-v2pro-20250604\SoVITS_weights_v2ProPlus\Kirie_e8_s1040.pth"

REF_AUDIO_PATH = r"G:\Projects\gal_chara_tts\kiri_voice\voice\1_kiri1922.ogg"
REF_PROMPT_TEXT = "先生って、お堅いのね……たまに貴方の考えていることが、分からない……"
REF_PROMPT_LANG = "ja"

PUNCTUATION_CHARS = ['。', '！', '？', '，', '、', '.', '!', '?', ',', '\n']
SENTINEL = object()

MIN_SENTENCE_LENGTH = 8
MAX_TTS_THREADS = 1

def load_tts_model():
    print(f"正在唤醒雾枝的语音模块...")
    try:
        requests.get(f"{TTS_API_URL}/set_gpt_weights", params={"weights_path": GPT_MODEL_PATH}, timeout=10)
        requests.get(f"{TTS_API_URL}/set_sovits_weights", params={"weights_path": SOVITS_MODEL_PATH}, timeout=10)
        print("✅ 语音模块加载完毕！\n")
        return True
    except Exception as e:
        print(f"❌ 模型挂载失败: {e}")
        return False

# ==========================================
# 线程任务：单一短句的 TTS 请求 (静默收集日志)
# ==========================================
def fetch_tts_audio(sentence, sentence_audio_queue, chunk_idx, debug_stats):
    req_start = time.perf_counter()
    first_chunk_time = 0
    total_time = 0
    
    tts_params = {
        "ref_audio_path": REF_AUDIO_PATH,
        "prompt_text": REF_PROMPT_TEXT,
        "prompt_lang": REF_PROMPT_LANG,
        "text": sentence,
        "text_lang": TARGET_LANG,
        "text_split_method": "cut0",
        "streaming_mode": 2,
        "media_type": "raw",
    }
    
    try:
        res = requests.get(f"{TTS_API_URL}/tts", params=tts_params, timeout=30, stream=True)
        res.raise_for_status()
        
        is_first = True
        for chunk in res.iter_content(chunk_size=4096):
            if chunk:
                if is_first:
                    first_chunk_time = time.perf_counter() - req_start
                    is_first = False
                sentence_audio_queue.put(chunk)
                
        total_time = time.perf_counter() - req_start
        
    except Exception as e:
        sentence = f"[ERROR] {sentence}"
        print(f"\n❌ TTS 请求错误 ({sentence}): {e}")
    finally:
        sentence_audio_queue.put(SENTINEL)
        # 将诊断信息存入共享列表
        debug_stats.append({
            'idx': chunk_idx,
            'text': sentence,
            'ttfa': first_chunk_time,
            'total': total_time
        })

# ==========================================
# 文本清洗
# ==========================================
def clean_text_for_tts(text):
    text = text.replace('\n', ' ')
    text = re.sub(r'…。', '、', text)
    text = re.sub(r'[…・]+', '、', text)
    text = re.sub(r'\.{2,}', '、', text)
    text = re.sub(r'[、，,]{2,}', '、', text)
    return text.strip()

# ==========================================
# 线程 1: 文本处理与分发线程 (替代原LLM线程)
# ==========================================
def process_text_worker(user_text, playback_queue, tts_executor, debug_stats):
    buffer = user_text
    chunk_idx = 0 
    
    while buffer:
        split_idx = -1
        # 寻找在 MIN_SENTENCE_LENGTH 之后的第一个标点符号
        for i, char in enumerate(buffer):
            if i >= MIN_SENTENCE_LENGTH and char in PUNCTUATION_CHARS:
                split_idx = i
                while split_idx + 1 < len(buffer) and buffer[split_idx + 1] in PUNCTUATION_CHARS:
                    split_idx += 1
                break
        
        # 截取句子
        if split_idx != -1:
            sentence = buffer[:split_idx+1].strip()
            buffer = buffer[split_idx+1:]
        else:
            # 找不到标点或者剩下的字数不够长，直接把剩下的全部作为一句
            sentence = buffer.strip()
            buffer = ""
            
        if sentence:
            sentence_audio_queue = queue.Queue()
            playback_queue.put(sentence_audio_queue)
            
            cleaned_sentence = clean_text_for_tts(sentence)
            tts_executor.submit(fetch_tts_audio, cleaned_sentence, sentence_audio_queue, chunk_idx, debug_stats)
            
            chunk_idx += 1

    # 结束标记
    playback_queue.put(SENTINEL)

# ==========================================
# 线程 2: 顺序播放器线程
# ==========================================
def audio_player_worker(playback_queue):
    p = pyaudio.PyAudio()
    stream = p.open(format=pyaudio.paInt16, channels=1, rate=32000, output=True)

    while True:
        sentence_audio_queue = playback_queue.get()
        if sentence_audio_queue is SENTINEL:
            break

        # 先预读若干块再开始播，抹平 TTS 流内部的空洞
        pre_buffer = []
        PREBUFFER_CHUNKS = 3 
        
        while len(pre_buffer) < PREBUFFER_CHUNKS:
            chunk = sentence_audio_queue.get()
            if chunk is SENTINEL:
                break
            pre_buffer.append(chunk)
        
        for chunk in pre_buffer:
            stream.write(chunk)

        while True:
            chunk = sentence_audio_queue.get()
            if chunk is SENTINEL:
                break
            stream.write(chunk)

    stream.stop_stream()
    stream.close()
    p.terminate()

# ==========================================
# 🚀 交互式主程序
# ==========================================
def main():
    if not load_tts_model():
        return
        
    print("="*45)
    print("   直读控制台 (输入 'exit' 退出)   ")
    print("="*45)

    while True:
        user_input = input("\n[请输入要朗读的文字]: ").strip()
        if not user_input: continue
        if user_input.lower() in ["exit", "quit", "退出"]: break

        playback_queue = queue.Queue()
        debug_stats = [] 
        
        tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_TTS_THREADS)

        # 启动处理线程和播放线程
        t_process = threading.Thread(target=process_text_worker, args=(user_input, playback_queue, tts_executor, debug_stats))
        t_player = threading.Thread(target=audio_player_worker, args=(playback_queue,))

        t_player.start()
        t_process.start()

        # 等待播报完毕
        t_process.join()
        t_player.join()
        tts_executor.shutdown(wait=True)
        
        # 播报完毕后，统一格式化并打印调试信息
        debug_stats.sort(key=lambda x: x['idx']) 
        
        text_line = " | ".join([s['text'].replace('\n', '\\n') for s in debug_stats])
        time_line = " | ".join([f"首包{s['ttfa']:.2f}s(总{s['total']:.2f}s)" for s in debug_stats])
        
        print(f"[DEBUG ✂️] 切片: {text_line}")
        print(f"[DEBUG ⏱️] 耗时: {time_line}")

if __name__ == "__main__":
    main()