import google.generativeai as genai
from tqdm import tqdm
import time
import os
import asyncio
import re
import hashlib
import base64
import numpy as np
import requests
import logging
from PIL import Image
from backend.app.services.utility.text import *
import soundfile as sf

# ─────────────────────────────────────────────────────────────
# Gemini 內容快取（記憶體層）
# key: MD5(text + language + prompt_prefix), value: 生成文字
# 伺服器重啟後清空；如需跨重啟持久化請改用 Redis / shelve。
# ─────────────────────────────────────────────────────────────
_gemini_response_cache: dict = {}
logger = logging.getLogger("video_abstract")

def get_google_generative_model_name() -> str:
	return os.getenv("GOOGLE_GENERATIVE_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash")).strip() or "gemini-2.5-flash"


def get_google_generative_endpoint(model_name: str) -> str:
	endpoint = os.getenv("GOOGLE_GENERATIVE_ENDPOINT", "").strip()
	if endpoint:
		if "{model}" in endpoint:
			return endpoint.format(model=model_name)
		if endpoint.rstrip("/").endswith(":generateContent"):
			return endpoint
		return f"{endpoint.rstrip('/')}/models/{model_name}:generateContent"
	return f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"


def get_google_generative_fallback_model_name() -> str:
	return os.getenv("GOOGLE_GENERATIVE_MODEL_FALLBACK", "gemini-2.5-pro").strip() or "gemini-2.5-pro"


def use_google_fallback_model() -> bool:
	return str(os.getenv("GOOGLE_GENERATIVE_FALLBACK_ENABLED", "true")).strip().lower() in {"1", "true", "yes", "on"}


def use_llm_response_cache() -> bool:
	return str(os.getenv("LLM_ENABLE_CACHE", "false")).strip().lower() in {"1", "true", "yes", "on"}


def get_openai_model_name() -> str:
	return os.getenv("OPENAI_MODEL", os.getenv("EXTERNAL_LLM_MODEL", "gpt-4.1-mini")).strip() or "gpt-4.1-mini"


def get_anthropic_model_name() -> str:
	return os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest").strip() or "claude-3-5-sonnet-latest"


def get_openrouter_model_name() -> str:
	return os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1-mini").strip() or "openai/gpt-4.1-mini"


def get_xai_model_name() -> str:
	return os.getenv("XAI_MODEL", "grok-3-mini").strip() or "grok-3-mini"


def get_groq_model_name() -> str:
	return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"


def get_llm_model_name(provider: str) -> str:
	if provider == "google":
		return get_google_generative_model_name()
	if provider == "anthropic":
		return get_anthropic_model_name()
	if provider == "openrouter":
		return get_openrouter_model_name()
	if provider == "xai":
		return get_xai_model_name()
	if provider == "groq":
		return get_groq_model_name()
	return get_openai_model_name()


def get_chat_completion_endpoint(provider: str) -> str:
	defaults = {
		"openai": "https://api.openai.com/v1/chat/completions",
		"openrouter": "https://openrouter.ai/api/v1/chat/completions",
		"xai": "https://api.x.ai/v1/chat/completions",
		"groq": "https://api.groq.com/openai/v1/chat/completions",
	}
	override_names = {
		"openai": ("OPENAI_ENDPOINT", "EXTERNAL_LLM_ENDPOINT"),
		"openrouter": ("OPENROUTER_ENDPOINT",),
		"xai": ("XAI_ENDPOINT",),
		"groq": ("GROQ_ENDPOINT",),
	}
	for env_name in override_names.get(provider, ()):
		value = os.getenv(env_name, "").strip()
		if value:
			return value
	return defaults.get(provider, defaults["openai"])


def get_anthropic_endpoint() -> str:
	return os.getenv("ANTHROPIC_ENDPOINT", "https://api.anthropic.com/v1/messages").strip()


def get_llm_api_key() -> str:
	return (
		os.getenv("api_key", "")
		or os.getenv("GOOGLE_API_KEY", "")
		or os.getenv("GEMINI_API_KEY", "")
		or os.getenv("OPENAI_API_KEY", "")
		or os.getenv("ANTHROPIC_API_KEY", "")
		or os.getenv("OPENROUTER_API_KEY", "")
		or os.getenv("XAI_API_KEY", "")
		or os.getenv("GROQ_API_KEY", "")
		or os.getenv("EXTERNAL_LLM_API_KEY", "")
	).strip()


def infer_llm_provider_from_key(api_key: str | None = None) -> str:
	key = (api_key or get_llm_api_key()).strip()
	if not key:
		return "missing"
	lower_key = key.lower()
	if lower_key.startswith("sk-ant"):
		return "anthropic"
	if lower_key.startswith("sk-or-v1"):
		return "openrouter"
	if lower_key.startswith("xai-"):
		return "xai"
	if lower_key.startswith("gsk_"):
		return "groq"
	if lower_key.startswith("sk"):
		return "openai"
	if key.startswith("AI") or key.startswith("AQ."):
		return "google"
	return "unknown"


def get_llm_config_summary() -> dict:
	api_key = get_llm_api_key()
	provider = infer_llm_provider_from_key(api_key)
	return {
		"provider": provider,
		"model": get_llm_model_name(provider),
		"endpoint": (
			"https://generativelanguage.googleapis.com"
			if provider == "google"
			else get_anthropic_endpoint()
			if provider == "anthropic"
			else get_chat_completion_endpoint(provider)
		),
		"google_model": get_google_generative_model_name(),
		"google_fallback_model": get_google_generative_fallback_model_name(),
		"google_fallback_enabled": use_google_fallback_model(),
		"google_endpoint": "https://generativelanguage.googleapis.com",
		"openai_endpoint": get_chat_completion_endpoint("openai"),
		"openai_model": get_openai_model_name(),
		"anthropic_endpoint": get_anthropic_endpoint(),
		"anthropic_model": get_anthropic_model_name(),
		"openrouter_endpoint": get_chat_completion_endpoint("openrouter"),
		"openrouter_model": get_openrouter_model_name(),
		"has_key": bool(api_key),
	}


def _is_suspect_script_response(text: str) -> bool:
	s = str(text or "").strip().lower()
	if not s:
		return True
	if len(s) < 24:
		return True
	bad_patterns = [
		"請提供您需要改寫的文字內容",
		"請提供需要改寫的文字",
		"please provide the text",
		"please provide the content",
	]
	return any(p in s for p in bad_patterns)

async def gemini_chat(text_array=None, script=None, api_key=None, max_retries=5, language=None, model_name_override=None):
	"""
	非同步並行 Gemini API 呼叫。

	優化項目：
	  1. asyncio.gather() 同時發送所有頁面請求
	  2. Semaphore(3) 限制最大並發數，防止 429 Rate Limit
	  3. MD5 內容快取：相同文字直接回傳，跳過 API 呼叫
	"""
	if text_array is None or script is None:
		raise ValueError("script or text_array can't be None")

	if api_key is None or not isinstance(api_key, str) or len(api_key.strip()) == 0:
		raise ValueError("`api_key` must be provided and non-empty (a single string)")

	# Determine actual language ('zh' or 'en')
	actual_language = 'zh'  # default to Chinese
	if language:
		lang_str = str(language).lower()
		if 'en' in lang_str or lang_str.startswith('en-'):
			actual_language = 'en'
		elif 'zh' in lang_str or 'cn' in lang_str or lang_str.startswith('zh-'):
			actual_language = 'zh'
	
	print(f"[GEMINI] Content language set to: {actual_language}")
	
	# Language-specific system prompts
	if actual_language == 'en':
		system_prompt = '''You are a professional presentation script writer.
Please rewrite the provided content into a natural spoken presentation script.

Rules:
1. Conversational: Convert written language to natural spoken English with simple sentence structures.
2. Natural tone: Like speaking face-to-face, not reading an article.
3. Complete sentences: Each sentence should stand alone, avoid fragmented content.
4. Logical flow: Ensure smooth transitions between sentences.
5. Clear pronunciation: Avoid overly complex vocabulary.
6. Engaging: Include rhetorical questions or emphasis to maintain listener interest.
7. Zero preamble mode: Output the rewritten script directly. NEVER include any opening remarks (like "Here is your script", "Rewritten as follows") or closing explanations.
8. Keep each page's script under 150 words.
9. Keep proper nouns, paper titles, author names, technical terms in original form; do not force translation.
10. If this page is a table-of-contents/agenda page, only give a brief transition-style overview, do not explain each item in detail.
11. End with a complete sentence. Do not end with ':' or an unfinished list.

CRITICAL: Output MUST be in English only. No Chinese characters allowed.

Content to rewrite:'''
	else:
		system_prompt = '''你是一位專業的演講稿撰寫人。請將我接下來提供的文字內容改寫為一份自然的口頭講稿。
遵守以下規則：
1. 口語化： 將書面語轉換為適合朗讀的口語，句子結構要簡單、節奏感強。
2. 語氣自然： 像是在與人面對面交談，而非朗讀文章。
3. 零廢話模式： 直接輸出改寫後的講稿內容。絕對不要輸出任何開場白（如「好的，這是您的講稿」、「改寫如下」）或結尾的解釋。
4. 完整句子： 每句話要獨立完整，避免碎片化內容。
5. 邏輯流暢： 確保句子間的銜接自然流暢。
6. 專有名詞（人名、論文名、術語、方法名）盡量保留原文，不要硬翻。
7. 若此頁是目錄/大綱頁，僅做導覽式帶過，不逐條展開細講。
8. 結尾必須是完整句，不可用「：」或未完成條列作結。

需改寫的內容如下：'''

	# ─────────────────────────────────────────────────────────────
	# 🚀 Step A: 設定 Gemini 客戶端與並發控制
	# ─────────────────────────────────────────────────────────────
	model_name = (model_name_override or get_google_generative_model_name()).strip()
	genai.configure(api_key=api_key)
	model = genai.GenerativeModel(model_name)
	print(f"[GEMINI] Model: {model_name}")

	# Semaphore: 最多同時 3 個並行請求，防止觸發 429 Rate Limit
	semaphore = asyncio.Semaphore(3)

	# ─────────────────────────────────────────────────────────────
	# 🔑 Step B: 定義單頁非同步處理函式（含快取 + retry）
	# ─────────────────────────────────────────────────────────────
	async def _generate_one_page(idx: int, text: str):
		"""單頁 Gemini 呼叫：先查快取，未命中則申請 Semaphore 後並行呼叫"""
		if not str(text or "").strip():
			return idx, ""

		# 1️⃣ MD5 快取查詢（依文字內容 + 語言）
		cache_key = hashlib.md5(f"{text}|{actual_language}".encode("utf-8")).hexdigest()
		if use_llm_response_cache() and cache_key in _gemini_response_cache:
			print(f"[GEMINI] 快取命中，跳過 API 請求 (頁 {idx + 1}/{len(text_array)})")
			return idx, _gemini_response_cache[cache_key]

		# 2️⃣ 申請 Semaphore 槽位後發送 API（確保最多 3 個並行）
		async with semaphore:
			loop = asyncio.get_event_loop()
			for retry in range(max_retries):
				try:
					# run_in_executor 讓 blocking 的 generate_content 不阻塞 event loop
					response = await loop.run_in_executor(
						None,
						lambda t=text: model.generate_content(f'{system_prompt} {t}')
					)
					generated_text = remove_markdown(response.text)
					logger.info("[LLM][Gemini][%s][p%s] raw_len=%s preview=%r", model_name, idx + 1, len(generated_text), generated_text[:180])

					# 語言驗證（沿用原有邏輯）
					has_chinese = bool(re.search(r'[\u4e00-\u9fff]', generated_text))
					if actual_language == 'en' and has_chinese:
						chinese_count = len(re.findall(r'[\u4e00-\u9fff]', generated_text))
						chinese_ratio = chinese_count / max(len(generated_text), 1)
						if chinese_ratio > 0.1:
							print(f"⚠️ [GEMINI] 頁 {idx+1} 回傳 {chinese_ratio:.1%} 中文但要求英文，重試...")
							await asyncio.sleep(2)
							continue  # 重試
						else:
							print(f"ℹ[GEMINI] 頁 {idx+1}: 少量中文字元 ({chinese_ratio:.1%})，可接受")

					# 3️⃣ 存入快取
					if use_llm_response_cache():
						_gemini_response_cache[cache_key] = generated_text
					print(f"[GEMINI] 頁 {idx+1}/{len(text_array)} 完成 (retry={retry})")
					return idx, generated_text

				except Exception as e:
					error_message = str(e)
					if "RESOURCE_EXHAUSTED" in error_message or "429" in error_message or "QUOTA_EXCEEDED" in error_message:
						wait_time = min(2 ** retry * 5, 300)
						print(f"⚠️ [GEMINI] Rate limit (頁 {idx+1})，等待 {wait_time}s (retry {retry+1}/{max_retries})")
						await asyncio.sleep(wait_time)
					elif "503" in error_message or "UNAVAILABLE" in error_message or "overloaded" in error_message:
						wait_time = min(5 * (retry + 1), 120)
						print(f"⚠️ [GEMINI] 服務不可用 (頁 {idx+1})，等待 {wait_time}s...")
						await asyncio.sleep(wait_time)
					elif "500" in error_message or "INTERNAL" in error_message:
						wait_time = min(3 * (retry + 1), 60)
						print(f"⚠️ [GEMINI] 內部錯誤 (頁 {idx+1})，等待 {wait_time}s...")
						await asyncio.sleep(wait_time)
					else:
						if retry < max_retries - 1:
							await asyncio.sleep(min(2 * (retry + 1), 10))
						else:
							print(f"❌ [GEMINI] 頁 {idx+1} 持續錯誤，放棄: {error_message}")
							raise

			raise Exception(f"❌ Max retries ({max_retries}) 已用盡 (頁 {idx + 1})")

	# ─────────────────────────────────────────────────────────────
	# 🚀 Step C: asyncio.gather() 同時發送所有頁面請求
	# ─────────────────────────────────────────────────────────────
	print(f"[GEMINI] 並行發送 {len(text_array)} 頁請求（Semaphore=3）...")
	tasks = [_generate_one_page(idx, text) for idx, text in enumerate(text_array)]
	raw_results = await asyncio.gather(*tasks, return_exceptions=True)

	# 檢查是否有任何頁面失敗
	for result in raw_results:
		if isinstance(result, Exception):
			raise result

	# 依原始頁碼順序整理結果
	response_array_of_text = [None] * len(text_array)
	for result in raw_results:
		page_idx, page_text = result
		response_array_of_text[page_idx] = page_text

	print(f"[GEMINI] 所有 {len(text_array)} 頁處理完成")
	return response_array_of_text


def _build_script_prompt(page_text: str, language: str) -> str:
	if language == "en":
		return (
			"You are a professional presentation script writer. Rewrite the page content into concise, natural spoken English. "
			"Output only the final script content, no preamble.\n\n"
			f"Page content:\n{page_text}"
		)
	return (
		"你是一位專業的演講稿撰寫人。請把該頁內容改寫成口語自然、簡潔流暢的中文講稿。"
		"只輸出最終講稿，不要任何前言或說明。\n\n"
		f"頁面內容：\n{page_text}"
	)


async def chat_completion_llm_chat(text_array=None, language=None, api_key=None, provider="openai"):
	if text_array is None:
		raise ValueError("text_array can't be None")
	api_key = (api_key or get_llm_api_key()).strip()
	endpoint = get_chat_completion_endpoint(provider)
	model = get_llm_model_name(provider)
	timeout_sec = int(os.getenv("EXTERNAL_LLM_TIMEOUT_SEC", "90"))
	if not api_key:
		raise ValueError("LLM api_key is empty")

	lang = "en" if str(language or "").lower().startswith("en") else "zh"

	def _call_one(text: str) -> str:
		if not str(text or "").strip():
			return ""
		payload = {
			"model": model,
			"messages": [
				{"role": "user", "content": _build_script_prompt(str(text or ""), lang)},
			],
			"temperature": 0.4,
		}
		resp = requests.post(
			endpoint,
			headers={
				"Authorization": f"Bearer {api_key}",
				"Content-Type": "application/json",
			},
			json=payload,
			timeout=timeout_sec,
		)
		resp.raise_for_status()
		data = resp.json()
		return str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()

	loop = asyncio.get_event_loop()
	results = []
	for t in text_array:
		content = await loop.run_in_executor(None, lambda x=t: _call_one(x))
		results.append(remove_markdown(content))
	return results


async def anthropic_llm_chat(text_array=None, language=None, api_key=None):
	if text_array is None:
		raise ValueError("text_array can't be None")
	api_key = (api_key or get_llm_api_key()).strip()
	endpoint = get_anthropic_endpoint()
	model = get_anthropic_model_name()
	timeout_sec = int(os.getenv("EXTERNAL_LLM_TIMEOUT_SEC", "90"))
	if not api_key:
		raise ValueError("LLM api_key is empty")

	lang = "en" if str(language or "").lower().startswith("en") else "zh"

	def _call_one(text: str) -> str:
		if not str(text or "").strip():
			return ""
		payload = {
			"model": model,
			"max_tokens": 800,
			"temperature": 0.4,
			"messages": [
				{"role": "user", "content": _build_script_prompt(str(text or ""), lang)},
			],
		}
		resp = requests.post(
			endpoint,
			headers={
				"x-api-key": api_key,
				"anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01"),
				"Content-Type": "application/json",
			},
			json=payload,
			timeout=timeout_sec,
		)
		resp.raise_for_status()
		data = resp.json()
		content_blocks = data.get("content", [])
		if content_blocks and isinstance(content_blocks, list):
			return str(content_blocks[0].get("text", "")).strip()
		return ""

	loop = asyncio.get_event_loop()
	results = []
	for t in text_array:
		content = await loop.run_in_executor(None, lambda x=t: _call_one(x))
		results.append(remove_markdown(content))
	return results


async def external_llm_chat(text_array=None, language=None, api_key=None, provider="openai"):
	if provider == "anthropic":
		return await anthropic_llm_chat(text_array=text_array, language=language, api_key=api_key)
	return await chat_completion_llm_chat(text_array=text_array, language=language, api_key=api_key, provider=provider)


async def generate_presentation_scripts(text_array=None, script=None, api_key=None, language=None):
	"""
	Entry point for page-script generation.
	- AI... keys use Google Gemini.
	- sk... keys use OpenAI-compatible chat completions.
	- sk-ant... keys use Anthropic messages.
	- sk-or-v1... keys use OpenRouter chat completions.
	- xai... and gsk_... keys use OpenAI-compatible vendor endpoints.
	"""
	api_key = (api_key or get_llm_api_key()).strip()
	provider = infer_llm_provider_from_key(api_key)
	if provider in {"openai", "anthropic", "openrouter", "xai", "groq"}:
		return await external_llm_chat(text_array=text_array, language=language, api_key=api_key, provider=provider)
	if provider == "google":
		primary = await gemini_chat(text_array=text_array, script=script, api_key=api_key, language=language)
		if not use_google_fallback_model():
			return primary
		fallback_needed = [i for i, t in enumerate(primary or []) if _is_suspect_script_response(t)]
		if not fallback_needed:
			return primary
		fallback_model = get_google_generative_fallback_model_name()
		primary_model = get_google_generative_model_name()
		if fallback_model == primary_model:
			return primary
		fallback_texts = [str((text_array or [])[i] if i < len(text_array or []) else "") for i in fallback_needed]
		logger.warning("[LLM][Gemini] fallback triggered pages=%s primary=%s fallback=%s", fallback_needed, primary_model, fallback_model)
		fallback_out = await gemini_chat(
			text_array=fallback_texts,
			script=script,
			api_key=api_key,
			language=language,
			model_name_override=fallback_model,
		)
		merged = list(primary or [])
		for idx, page_idx in enumerate(fallback_needed):
			if idx < len(fallback_out):
				merged[page_idx] = fallback_out[idx]
		return merged
	if provider == "missing":
		raise ValueError("LLM api_key is missing")
	raise ValueError("Unsupported LLM api_key prefix. Use AI.../AQ.... (Google), sk... (OpenAI), sk-ant... (Claude), sk-or-v1... (OpenRouter), xai..., or gsk_...")


async def generate_presentation_scripts_from_images(
	image_paths=None,
	api_key=None,
	language=None,
	model_name_override=None,
):
	"""
	Vision fallback for image-only PDF pages (Google Gemini path).
	One request per page image.
	"""
	if image_paths is None:
		raise ValueError("image_paths can't be None")
	api_key = (api_key or get_llm_api_key()).strip()
	if not api_key:
		raise ValueError("LLM api_key is empty")

	model_name = (model_name_override or get_google_generative_fallback_model_name()).strip()
	genai.configure(api_key=api_key)
	model = genai.GenerativeModel(model_name)
	lang = str(language or "zh").lower()
	is_en = lang.startswith("en")
	if is_en:
		prompt = (
			"Read this slide image and write a concise spoken presentation script in English. "
			"Keep proper nouns, paper titles, author names and technical terms in original form. "
			"For outline pages, keep only a short transition. "
			"Output one complete paragraph only. Do not output bullets or preamble."
		)
	else:
		prompt = (
			"請閱讀這張投影片圖片，撰寫精簡且自然的中文口語講稿。"
			"專有名詞、人名、論文標題與技術術語請保留原文。"
			"若是目錄頁請簡短帶過，不逐條細講。"
			"只輸出單一完整段落，不要條列與前言。"
		)

	loop = asyncio.get_event_loop()
	results = []
	for idx, img_path in enumerate(image_paths):
		path = str(img_path or "").strip()
		if not path or not os.path.isfile(path):
			results.append("")
			continue
		def _call_one(p=path):
			with Image.open(p) as img:
				rsp = model.generate_content([prompt, img.convert("RGB")])
				txt = remove_markdown(str(getattr(rsp, "text", "") or "").strip())
				logger.info("[LLM][GeminiVision][%s][p%s] raw_len=%s preview=%r", model_name, idx + 1, len(txt), txt[:180])
				return txt
		try:
			content = await loop.run_in_executor(None, _call_one)
		except Exception as exc:
			logger.error("[LLM][GeminiVision] page %s failed: %s", idx + 1, exc)
			content = ""
		results.append(content)
	return results


async def generate_presentation_scripts_from_pdf_file(
	pdf_path: str,
	prompt: str,
	api_key=None,
	model_name_override=None,
	temperature: float = 0.85,
) -> str:
	"""
	Send the original PDF file directly to Gemini and return a single response string.
	"""
	if not pdf_path or not os.path.isfile(pdf_path):
		raise ValueError("pdf_path is invalid")
	api_key = (api_key or get_llm_api_key()).strip()
	if not api_key:
		raise ValueError("LLM api_key is empty")

	model_name = (model_name_override or get_google_generative_model_name()).strip()
	endpoint = get_google_generative_endpoint(model_name)
	timeout_sec = int(os.getenv("EXTERNAL_LLM_TIMEOUT_SEC", "180"))
	loop = asyncio.get_event_loop()

	def _call_once() -> str:
		with open(pdf_path, "rb") as fp:
			pdf_b64 = base64.b64encode(fp.read()).decode("ascii")
		payload = {
			"contents": [
				{
					"role": "user",
					"parts": [
						{"text": str(prompt or "")},
						{
							"inline_data": {
								"mime_type": "application/pdf",
								"data": pdf_b64,
							}
						},
					],
				}
			],
			"generationConfig": {
				"temperature": float(temperature),
			},
		}
		resp = requests.post(
			endpoint,
			headers={
				"Content-Type": "application/json",
				"x-goog-api-key": api_key,
			},
			json=payload,
			timeout=timeout_sec,
		)
		if not resp.ok:
			body = resp.text[:2000]
			logger.error(
				"[LLM][GeminiPDF][REST][%s] failed status=%s endpoint=%s body=%s",
				model_name,
				resp.status_code,
				endpoint,
				body,
			)
			raise RuntimeError(f"Gemini REST error {resp.status_code}: {body}")
		data = resp.json()
		parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
		text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()
		text = remove_markdown(text)
		logger.info("[LLM][GeminiPDF][REST][%s] raw_len=%s preview=%r", model_name, len(text), text[:180])
		return text

	return await loop.run_in_executor(None, _call_once)
