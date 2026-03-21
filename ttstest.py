import time
import json
import requests
import pyaudio
import threading
import queue
import concurrent.futures
import re

# ==========================================
# ⚙️ 参数配置区 
# ==========================================
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
OLLAMA_MODEL = "dolphin3"  
SYSTEM_PROMPT = "あなたは今、篝ノ霧枝というツンデレな二次元の吸血鬼少女よ。私の質問には、短くてツンデレ口調の日本語で答えなさい。"

TTS_API_URL = "http://127.0.0.1:9880" 
TARGET_LANG = "ja"                        
GPT_MODEL_PATH = r"D:\GPT-SoVITS-v2pro-20250604\GPT_weights_v2ProPlus\Kirie-e20.ckpt"
SOVITS_MODEL_PATH = r"D:\GPT-SoVITS-v2pro-20250604\SoVITS_weights_v2ProPlus\Kirie_e8_s1040.pth"

REF_AUDIO_PATH = r"G:\Projects\gal_chara_tts\kiri_voice\voice\1_kiri0007.ogg"
REF_PROMPT_TEXT = "私を、ジロジロ見ないでください"
REF_PROMPT_LANG = "ja"

PUNCTUATION_CHARS = ['。', '！', '？', '，', '、', '.', '!', '?', ',', '\n']
SENTINEL = object()

MIN_SENTENCE_LENGTH = 8
MAX_TTS_THREADS = 2

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
    finally:
        sentence_audio_queue.put(SENTINEL)
        # 将诊断信息存入共享列表（不直接打印，防止破坏控制台UI）
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
    text = re.sub(r'\.{2,}', '、', text)       # ← 新增：处理 ... 和 ....
    # text = re.sub(r'\*[^*]*\*', '', text)       # ← 新增：去掉 (*动作描写*) 这类括号内容，TTS 会把它念出来
    text = re.sub(r'[、，,]{2,}', '、', text)
    return text.strip()

# ==========================================
# 线程 1: LLM 主控线程
# ==========================================
def llm_worker(chat_history, playback_queue, tts_executor, debug_stats):
    payload = {"model": OLLAMA_MODEL, "messages": chat_history, "stream": True}
    buffer = ""
    full_text = ""
    chunk_idx = 0 
    
    print("\n[雾枝]: ", end="", flush=True)
    
    try:
        response = requests.post(OLLAMA_URL, json=payload, stream=True, timeout=60)
        response.raise_for_status()
        
        for line in response.iter_lines():
            if line:
                data = json.loads(line)
                token = data.get("message", {}).get("content", "")
                print(token, end="", flush=True)
                
                buffer += token
                full_text += token
                
                # 🌟 核心升级：基于索引的安全断句法
                # 寻找在 MIN_SENTENCE_LENGTH 之后的第一个标点符号
                split_idx = -1
                for i, char in enumerate(buffer):
                    if i >= MIN_SENTENCE_LENGTH and char in PUNCTUATION_CHARS:
                        # 找到标点后，继续往后看，把连续的标点（比如 "。！" 或 "……"）一次性全包进去
                        split_idx = i
                        while split_idx + 1 < len(buffer) and buffer[split_idx + 1] in PUNCTUATION_CHARS:
                            split_idx += 1
                        break
                
                if split_idx != -1:
                    # 精准切出完整的句子（刚好切在标点符号后）
                    sentence = buffer[:split_idx+1].strip()
                    
                    if sentence:
                        sentence_audio_queue = queue.Queue()
                        playback_queue.put(sentence_audio_queue)
                        
                        cleaned_sentence = clean_text_for_tts(sentence)
                        tts_executor.submit(fetch_tts_audio, cleaned_sentence, sentence_audio_queue, chunk_idx, debug_stats)
                        
                        chunk_idx += 1
                    
                    # 🌟 魔法发生：将切剩下的残余部分（比如被误带进来的 "と" 或 "そして"）留在 buffer 里，无缝衔接下一句！
                    buffer = buffer[split_idx+1:]
                    
        # 处理最后留在 buffer 里还没送去合成的尾巴
        if buffer.strip():
            sentence_audio_queue = queue.Queue()
            playback_queue.put(sentence_audio_queue)
            cleaned_sentence = clean_text_for_tts(buffer.strip())
            tts_executor.submit(fetch_tts_audio, cleaned_sentence, sentence_audio_queue, chunk_idx, debug_stats)
            
    except Exception as e:
        print(f"\n❌ LLM 请求错误: {e}")
        
    print() # 大模型输出完毕后换行
    chat_history.append({"role": "assistant", "content": full_text})
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

        # 🌟 先预读若干块再开始播，抹平 TTS 流内部的空洞
        pre_buffer = []
        PREBUFFER_CHUNKS = 3  # 根据实际延迟调整
        
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
    print("   控制台 (输入 'exit' 退出)   ")
    print("="*45)
    
    chat_history = [{"role": "system", "content": SYSTEM_PROMPT}]

    while True:
        user_input = input("\n[你]: ").strip()
        if not user_input: continue
        if user_input.lower() in ["exit", "quit", "退出"]: break
            
        chat_history.append({"role": "user", "content": user_input})

        playback_queue = queue.Queue()
        debug_stats = [] # 🌟 用于悄悄收集当前对话所有的调试信息
        
        tts_executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_TTS_THREADS)

        # 启动线程，将 debug_stats 传进去
        t_llm = threading.Thread(target=llm_worker, args=(chat_history, playback_queue, tts_executor, debug_stats))
        t_player = threading.Thread(target=audio_player_worker, args=(playback_queue,))

        t_player.start()
        t_llm.start()

        # 等待播报完毕
        t_llm.join()
        t_player.join()
        tts_executor.shutdown(wait=True)
        
        # 🌟 播报完毕后，统一格式化并打印调试信息
        debug_stats.sort(key=lambda x: x['idx']) # 按原始切分顺序排序
        
        # 将文本里的换行符替换为肉眼可见的 \n 方便排错
        text_line = " | ".join([s['text'].replace('\n', '\\n') for s in debug_stats])
        time_line = " | ".join([f"首包{s['ttfa']:.2f}s(总{s['total']:.2f}s)" for s in debug_stats])
        
        print(f"\n[DEBUG ✂️] 切片: {text_line}")
        print(f"[DEBUG ⏱️] 耗时: {time_line}")

if __name__ == "__main__":
    main()