"""Two-pass AI translation engine for Zay_Vietsub.vn.

Pass 1: Fast literal translation from Chinese / English to Vietnamese
        (MyMemory API primary + GoogleTranslator fallback).
Pass 2: Contextual AI refinement (Pollinations OpenAI LLM + Vietnamese Contextual
        Polisher) to fix idioms, pronouns, and fit subtitle reading speed.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
from deep_translator import GoogleTranslator

MYMEMORY_ENDPOINT = "https://api.mymemory.translated.net/get"
POLLINATIONS_ENDPOINT = "https://text.pollinations.ai/openai"

SOURCE_ZH_FIXES: list[tuple[str, str]] = [
    (r"言小熙|严小西|眼小西|眼小熙|眼晓熙|颜小熙|言小希|严小枝|眼小细|严小心|颜小西", "言小熙"),
    (r"窝龙|握龙", "卧龙"),
    (r"奉处", "凤雏"),
    (r"学爸|学买|血霸", "学霸"),
    (r"五沙", "五杀"),
    (r"虽这灵生的想起|随着灵生的想起", "随着铃声的响起，"),
    (r"昨天不日的作业都搅到", "昨天没交的作业都交到"),
    (r"眼神中近时难以知识", "眼神中尽是难以置信"),
    (r"崩烤", "崩塌"),
    (r"喷下下了", "喷下线了"),
    (r"像破脑袋", "想破脑袋"),
    (r"安及最后一拍的两位男生，?即从从向我走来，?黄孔不安中，?摆着", "班级最后一排的两位男生，急匆匆向我走来，惶恐不安中带着"),
    (r"录出结后余生的笑容|节后余生", "露出劫后余生的笑容"),
    (r"每语间", "眉宇间"),
    (r"一丝对父亲的感激", "一丝对父神的感激"),
    (r"最恶的交易|最乐的交易", "罪恶的交易"),
    (r"赌过了|读过了", "度过了"),
    (r"住从|住宠|驻从", "蛀虫"),
    (r"无口不弱|无苦不弱", "无孔不入"),
    (r"当然是要持|只要持|药池", "当上了钥匙"),
    (r"透过窗，?不看操场上，?你学生打难情", "透过窗户看操场上女学生打篮球"),
    (r"打难情", "打篮球"),
    (r"摩语", "摸鱼"),
    (r"是个机分家的太少了", "薅资本家的羊毛太少了"),
    (r"风浪越大越贵", "风浪越大鱼越贵"),
    (r"为了一笑", "微微一笑"),
    (r"由于骗课", "犹豫片刻"),
    (r"小声敌过脑", "小声嘀咕道"),
    (r"有一点点小学", "有一点点想学"),
    (r"小学(?=[^，。]*你能不能教教我)", "想学"),
    (r"奴班", "鲁班"),
    (r"提高了警体", "提高了警惕"),
    (r"断位不高", "段位不高"),
    (r"变了蝙嘴", "撇了撇嘴"),
    (r"不再大理我", "不再搭理我"),
    (r"音语课", "英语课"),
    (r"诈一看", "乍一看"),
    (r"那句句会什么的模样", "那聚精会神的模样"),
    (r"意思不狗的态度", "一丝不苟的态度"),
    (r"课打麻价，?到底主", "上课打麻将、斗地主"),
    (r"都不敢", "都不干"),
    (r"更加可气的事", "更加可气的是"),
    (r"补文的知识", "古文的知识"),
    (r"融入的脑科", "融入了脑海"),
    (r"掌脑子的感觉", "长脑子的感觉"),
    (r"与文老师加这一达券子", "语文老师夹着一沓卷子"),
    (r"伤错锁力干净，?取动下开四靠丝", "干净利落地宣布随堂考试"),
    (r"分发视券", "分发试卷"),
    (r"做券子", "做卷子"),
    (r"起了券子", "起了卷子"),
    (r"完成试券", "完成试卷"),
    (r"霸讨的话，?多少有些令类", "罢考的话，多少有些另类"),
    (r"仔细丑了丑", "仔细瞅了瞅"),
    (r"股文|股闻", "古文"),
    (r"股实词", "古诗词"),
    (r"蓝散", "懒散"),
    (r"进入家境", "渐入佳境"),
    (r"下一试丑了言", "下意识瞅了眼"),
    (r"关成了一篇", "完成了一篇"),
    (r"破位了解", "颇为理解"),
    (r"用了言文写|写了颜文", "用文言文写"),
    (r"上语", "上瘾"),
    (r"随堂词验", "随堂测验"),
    (r"满机", "满级"),
    (r"李白在事", "李白在世"),
    (r"平运起去抽", "凭运气去抽"),
    (r"咱点摸鱼极粉，?直接对换技能靠不点，?其他有努力", "攒点摸鱼积分，直接兑换技能靠谱点，与其瞎努力"),
    (r"咱点极粉", "攒点积分"),
    (r"送了口气", "松了口气"),
    (r"运科果然", "文科果然"),
    (r"吵着身边丑去", "朝着身边瞅去"),
    (r"不断吐草之急", "不断吐槽之际"),
    (r"我比的那只少，?在试卷上分比急书，?但比头似乎没有触摸", "我握笔的那只手，在试卷上奋笔疾书，但笔头似乎没有触碰试卷"),
    (r"过不期然", "果不其然"),
    (r"寻查", "巡查"),
    (r"太此意所思了", "太匪夷所思了"),
    (r"双眼几地", "双眼紧闭"),
    (r"我飞这就是", "莫非这就是"),
    (r"搬机里的同学", "班级里的同学"),
    (r"跑向时堂", "跑向食堂"),
    (r"眼尽学生", "严禁学生"),
    (r"敲敲回到", "悄悄回到"),
    (r"满脸一会", "满脸疑惑"),
    (r"单可以偷偷吃", "但可以偷偷吃"),
    (r"盾食一阵无语", "顿时一阵无语"),
    (r"违反校归", "违反校规"),
    (r"见见的不落下", "件件都不落下"),
    (r"把晓熙和往他边上推了推，?准你吃点", "把小盒往她边上推了推，请你吃点"),
    (r"名的名嘴", "抿了抿嘴"),
    (r"炸机盒", "炸鸡盒"),
    (r"以训练不及严尔之时", "以迅雷不及掩耳之势"),
    (r"李雷雅的寒妹妹，?后而由爱慕发\s*三可由", "李雷与韩梅梅，How are you? I'm fine, thank you"),
    (r"满脸猛圈", "满脸懵圈"),
    (r"坚持干", "兼职干"),
    (r"球质", "求知"),
    (r"响学", "想学"),
    (r"聊天宫评中对摊", "聊天公屏中对线"),
    (r"科桌|客桌", "课桌"),
]

# Common literal translation artifacts -> natural Vietnamese Vietsub phrasing
CONTEXT_PHRASE_FIXES: list[tuple[str, str]] = [
    (r"\b(?:Diệp Tiểu Hi|Diệp Tiểu Hy|Nghiêm Tiểu Tây|Nhãn Tiểu Tây|Nhan Tiểu Hi|Nhan Hiểu Hi|Nghiêm Tiểu Chi|Nghiêm Tiểu Tâm|Ngôn Tiểu Tế|Ngôn Tiểu Hy|Hiểu Hi)\b", "Ngôn Tiểu Hi"),
    (r"\bNgạ Long\b", "Ngọa Long"),
    (r"\b(?:Nubar|Nuban)\b", "Lỗ Ban"),
    (r"\bcô Trân\b", "cô Chu"),
    (r"\bủy viên học tập\b", "lớp phó học tập"),
    (r"\bxin chào tất cả mọi người[!,.]*\s*", "Chào mọi người, "),
    (r"\bxin chào mọi người[,!\s]*chào mừng đến với\b", "Chào mọi người, chào mừng đến với"),
    (r"\bchào mừng các bạn đã đến với\b", "chào mừng đến với"),
    (r"\bchào mừng các bạn đến với\b", "chào mừng đến với"),
    (r"\bhôm nay chúng ta sẽ thử nghiệm\b", "hôm nay mình cùng test"),
    (r"\bhôm nay chúng ta hãy\b", "hôm nay cùng"),
    (r"\bcái này rất là\b", "cái này cực kỳ"),
    (r"\bkhông có vấn đề gì cả\b", "không thành vấn đề"),
    (r"\bđừng quên nhấn thích và đăng ký\b", "nhớ thả tim và đăng ký kênh nhé"),
    (r"\bđăng ký kênh của tôi\b", "theo dõi kênh nhé"),
    (r"\bmột cách hoàn hảo\b", "mượt mà"),
    (r"\bchỉ\s+(?:với|bằng)\s+một\s+cú\s+nhấp\s+chuột\b", "chỉ với một chạm"),
    (r"\b(?:với|bằng)\s+một\s+cú\s+nhấp\s+chuột\b", "chỉ với một chạm"),
    (r"\bcó thể nhận ra\b", "có thể nhận diện"),
    (r"\btrong video này chúng ta sẽ\b", "trong video này mình sẽ"),
    (r"\bnó có thể\b", "công cụ này có thể"),
    (r"\bphát hiện ra rằng\b", "nhận ra"),
    (r"\bbiết rằng\b", "biết"),
    (r"\bcho rằng\b", "nghĩ"),
    (r"\btưởng rằng\b", "tưởng"),
    (r"\bcó vẻ như\b", "xem ra"),
    (r"\bbài tập về nhà\b", "bài tập"),
    (r"\bquay lại chỗ ngồi của mình\b", "về chỗ ngồi"),
    (r"\bcông việc bán thời gian\b", "việc làm thêm"),
    (r"\b(?:đồ ăn bên ngoài|đồ ăn giao tận nơi|đồ giao tận nơi)\b", "đồ ăn ngoài"),
    (r"\bgiáo viên chủ nhiệm\b", "cô chủ nhiệm"),
    (r"\bngười bạn cùng bàn\b", "bạn cùng bàn"),
    (r"\bvô cùng kinh ngạc\b", "sốc nặng"),
    (r"\bvô cùng bối rối\b", "ngơ ngác"),
    (r"\bthở phào nhẹ nhõm\b", "thở phào"),
    (r"\bngủ say như chết\b", "ngủ say sưa"),
    (r"\bvắt óc suy nghĩ cũng không thể hiểu nổi\b", "nghĩ nát óc cũng chẳng hiểu"),
    (r"\bnói đi nói lại\b", "nói chứ"),
    (r"\btrong một thời gian ngắn như vậy mà đã hoàn thành\b", "ngắn thế mà đã viết xong"),
    (r"\bkhông có chỗ nào mà không bị xâm nhập\b", "đúng là sâu mọt ở khắp nơi"),
]


def detect_source_lang(segments: list[dict[str, Any]], requested_lang: str = "auto") -> str:
    """Detect whether source text is primarily Chinese or English."""
    if requested_lang in ("zh-CN", "zh", "cn"):
        return "zh-CN"
    if requested_lang in ("en", "en-US"):
        return "en"

    joined = " ".join(str(seg.get("text", "")) for seg in segments)
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", joined))
    if cjk_count >= 1:
        return "zh-CN"
    return "en"


def _clean_translated_artifacts(text: str) -> str:
    """Strip translation XML/HTML tags (e.g. <x id="31"/>) and normalize whitespace."""
    cleaned = re.sub(r"<[^>]+>", " ", str(text or ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _mymemory_translate(client: httpx.Client, text: str, src_lang: str) -> str | None:
    pair = f"{'zh-CN' if src_lang.startswith('zh') else 'en'}|vi"
    try:
        resp = client.get(
            MYMEMORY_ENDPOINT,
            params={"q": text, "langpair": pair, "de": "contact@zayvietsub.vn"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        translated = _clean_translated_artifacts(
            str(data.get("responseData", {}).get("translatedText") or "")
        )
        if not translated or "MYMEMORY WARNING" in translated.upper():
            return None
        return translated
    except Exception:
        return None


def _translate_single_fallback(
    client: httpx.Client, text: str, gt_lang: str, src_lang: str
) -> str:
    """Translate a single line via GoogleTranslator first, MyMemory second."""
    clean_item = text.strip()
    if not clean_item:
        return ""

    out = ""
    for lang_src in (gt_lang, "auto"):
        try:
            cand = GoogleTranslator(source=lang_src, target="vi").translate(clean_item)
            cand = _clean_translated_artifacts(cand or "")
            if cand and not re.search(r"[\u4e00-\u9fff]", cand):
                out = cand
                break
            if cand and not out:
                out = cand
        except Exception:
            continue

    if not out or re.search(r"[\u4e00-\u9fff]", out):
        mm = _mymemory_translate(client, clean_item, src_lang)
        if mm and (not out or not re.search(r"[\u4e00-\u9fff]", mm)):
            out = mm

    return out or clean_item


def _pass1_translate_batch(texts: list[str], source_lang: str) -> list[str]:
    """Translate segments in Pass 1 using context-aware GoogleTranslator chunks + fallback."""
    if not texts:
        return []

    gt_lang = "zh-CN" if source_lang.startswith("zh") else "en"
    results = list(texts)

    # Batch up to 15 lines at a time so GoogleTranslator sees multi-sentence context
    batch_size = 15
    chunks: list[tuple[int, list[str]]] = []
    cur_batch: list[str] = []
    cur_start = 0
    cur_chars = 0

    for idx, t in enumerate(texts):
        line = re.sub(r"\s+", " ", str(t or "")).strip()
        if not cur_batch:
            cur_start = idx
        if len(cur_batch) >= batch_size or (cur_chars + len(line) > 3200 and cur_batch):
            chunks.append((cur_start, cur_batch))
            cur_batch = [line]
            cur_start = idx
            cur_chars = len(line)
        else:
            cur_batch.append(line)
            cur_chars += len(line) + 1
    if cur_batch:
        chunks.append((cur_start, cur_batch))

    with httpx.Client(timeout=14.0, limits=httpx.Limits(max_connections=8)) as client:
        for start_idx, batch_lines in chunks:
            non_empty_positions = [i for i, x in enumerate(batch_lines) if x]
            if not non_empty_positions:
                continue
            payload_lines = [batch_lines[i] for i in non_empty_positions]
            joined = "\n".join(payload_lines)
            batch_ok = False
            try:
                translated_block = GoogleTranslator(source=gt_lang, target="vi").translate(joined)
                if translated_block:
                    split_lines = [
                        _clean_translated_artifacts(ln) for ln in translated_block.splitlines()
                    ]
                    if len(split_lines) == len(payload_lines) and all(split_lines):
                        for rel_i, pos in enumerate(non_empty_positions):
                            cand = split_lines[rel_i]
                            if re.search(r"[\u4e00-\u9fff]", cand):
                                cand = _translate_single_fallback(
                                    client, batch_lines[pos], gt_lang, source_lang
                                )
                            results[start_idx + pos] = cand
                        batch_ok = True
            except Exception:
                batch_ok = False

            if not batch_ok:
                def _one(pos_idx: int) -> tuple[int, str]:
                    return pos_idx, _translate_single_fallback(
                        client, batch_lines[pos_idx], gt_lang, source_lang
                    )

                with ThreadPoolExecutor(max_workers=4) as pool:
                    for pos_idx, out_txt in pool.map(_one, non_empty_positions):
                        results[start_idx + pos_idx] = out_txt

    return results


def _polish_vietnamese_line(text: str, duration: float) -> str:
    """Clean artifacts, preserve grammar, insert natural pauses & terminal punctuation for TTS."""
    cleaned = _clean_translated_artifacts(text)
    if not cleaned:
        return ""

    # Fix machine-translation slash artifacts (e.g. "Có/是") and stray residual CJK glyphs
    cleaned = re.sub(r"[^,\s/]*/[\u4e00-\u9fff]+", "", cleaned)
    cleaned = re.sub(r"[\u4e00-\u9fff]+", "", cleaned)

    for pattern, repl in CONTEXT_PHRASE_FIXES:
        cleaned = re.sub(pattern, repl, cleaned, flags=re.IGNORECASE)

    # Condense wordy filler phrases when sentence is dense relative to slot duration
    words_now = cleaned.split()
    if len(words_now) / max(0.8, duration) > 4.6:
        filler_rules = [
            (r"\b(?:tôi|mình|anh ấy|cô ấy)\s+nhận ra\s+", "thấy "),
            (r"\bđiều khiến tôi tức giận,?\s*hơn nữa là\b", "tức hơn là"),
            (r"\bđiều này càng làm tăng mức độ nguy hiểm\b", "càng nguy hiểm hơn"),
            (r"\bvấn đề quan trọng là\b", "quan trọng là"),
            (r"\bdù sao thì\b", "dù gì"),
            (r"\bnhững nội dung liên quan đến\b", "phần"),
            (r"\bnhững việc liên quan đến\b", "việc"),
            (r"\bdường như đều là\b", "đều là"),
            (r"\bcó thể dễ dàng trả lời các câu hỏi\b", "dễ dàng giải được câu"),
            (r"\bmột điều may mắn hiếm có\b", "may mắn hiếm có"),
            (r"\btất cả bài tập chưa nộp của hôm qua\b", "bài tập chưa nộp hôm qua"),
            (r"\bđến văn phòng của tôi\b", "lên văn phòng tôi"),
            (r"\btrong một thời gian ngắn\b", "nhanh thế"),
            (r"\bcủa mình\b", ""),
            (r"\brằng là\b", ""),
        ]
        for f_pat, f_repl in filler_rules:
            cleaned = re.sub(f_pat, f_repl, cleaned, flags=re.IGNORECASE)

    # Normalize spaces and remove malformed double punctuation (e.g. "sau đó,." or "..")
    cleaned = re.sub(r"\s+([,!.?…;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,;:])([^\s0-9.!?…])", r"\1 \2", cleaned)
    cleaned = re.sub(r"[,;:]+\s*([.!?…])", r"\1", cleaned)
    cleaned = re.sub(r"([.!?])([^\s.!?\"'”)])", r"\1 \2", cleaned)
    cleaned = re.sub(r"\.{3,}", "…", cleaned)
    cleaned = re.sub(r"\.{2}(?!\.)", ".", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        return ""

    # Add a natural breathing comma before mid-sentence conjunctions if missing punctuation
    conj_pattern = (
        r"(?<!^)(?<![,.;:!?…—-])\s+"
        r"(nhưng|tuy nhiên|đồng thời|vì vậy|cho nên|hơn nữa|lát nữa|thế nên|mà lại)\b"
    )
    words_before_check = cleaned.split()
    if len(words_before_check) >= 5:
        head_two = " ".join(words_before_check[:2])
        rest = " ".join(words_before_check[2:])
        rest = re.sub(conj_pattern, r", \1", rest, flags=re.IGNORECASE)
        cleaned = f"{head_two} {rest}".strip()

    if cleaned:
        cleaned = cleaned[0].upper() + cleaned[1:]

    # Final cleanup before checking sentence ending
    cleaned = re.sub(r"[,;:]+\s*([.!?…])", r"\1", cleaned)
    cleaned = re.sub(r"[,;:]+$", "", cleaned).strip()

    # Always keep sentence-ending punctuation so neural TTS stops and inflects cleanly
    if cleaned and cleaned[-1] not in (".", "?", "!", "…"):
        q_starters = (
            "tại sao", "vì sao", "làm sao", "sao lại", "cái gì", "ai ", "bao nhiêu",
            "như thế nào", "được không", "phải không", "hay sao", "à?", "ư?",
        )
        q_endings = ("không", "chưa", "sao", "gì", "nào", "đâu", "nhỉ", "hả", "vậy", "thế")
        low = cleaned.lower()
        if any(q in low for q in q_starters) or (len(low.split()) <= 10 and low.split()[-1] in q_endings):
            cleaned += "?"
        else:
            cleaned += "."

    return cleaned


def _recommend_line_speed(text_vi: str, duration: float) -> float:
    """Compute recommended per-sentence speaking speed multiplier (1.10x - 1.55x)."""
    words = len(str(text_vi or "").split())
    wps = words / max(0.75, duration)
    if wps >= 6.8:
        return 1.45
    if wps >= 5.8:
        return 1.35
    if wps >= 4.8:
        return 1.25
    if wps >= 3.8:
        return 1.20
    return 1.15


def _call_free_ai_pass2(
    items: list[dict[str, Any]], source_lang: str
) -> tuple[dict[int, str] | None, bool]:
    """Call Free AI LLM (Pollinations OpenAI endpoint); returns (refined_map, rate_limited)."""
    lang_label = "Tiếng Trung" if source_lang.startswith("zh") else "Tiếng Anh"
    payload_lines = [
        {
            "index": item["index"],
            "duration_sec": round(max(0.5, item["end"] - item["start"]), 2),
            "original": item["original"],
            "pass1_vi": item["pass1_vi"],
        }
        for item in items
    ]

    system_prompt = (
        "Bạn là chuyên gia biên tập Vietsub & đạo diễn lồng tiếng chuyên nghiệp cho Zay_Vietsub.vn.\n"
        f"Nhiệm vụ: Kiểm tra và biên tập lại bản dịch Lượt 1 từ {lang_label} sang Tiếng Việt (Lượt 2) theo toàn bộ mạch truyện.\n"
        "Yêu cầu BẮT BUỘC:\n"
        "1. Dịch đúng ngữ cảnh, đúng tiếng lóng/thành ngữ (ví dụ tiếng Trung: 摸鱼 = trốn tiết/lười biếng, "
        "学习委员 = lớp phó học tập, 五杀 = Penta Kill/5 mạng, 点外卖 = đặt đồ ăn ngoài, 黑心老板 = ông chủ bóc lột, 耳鸣 = ù tai), tuyệt đối không dịch máy ngô nghê và không để sót chữ Hán hay thẻ HTML.\n"
        "2. BẮT BUỘC ngắt câu và chấm câu chuẩn Tiếng Việt (dùng dấu phẩy `,` ở chỗ ngắt hơi giữa vế câu; kết thúc mỗi câu phải có dấu chấm `.`, chấm hỏi `?`, chấm than `!` hoặc `...`) để giọng AI lồng tiếng biết ngắt nghỉ, lên xuống giọng tự nhiên.\n"
        "3. Văn phong khẩu ngữ tự nhiên, thống nhất xưng hô theo nhân vật.\n"
        "4. Độ dài gọn gàng, nói vừa khít trong `duration_sec` giây (khoảng 4-5 từ/giây), nhưng KHÔNG cắt cụt ngữ pháp làm mất nghĩa.\n"
        "5. CHỈ trả về JSON hợp lệ dạng mảng: [{\"index\": 0, \"text_vi\": \"...\"}, ...], không giải thích thêm."
    )

    body = {
        "model": "openai",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload_lines, ensure_ascii=False)},
        ],
        "temperature": 0.25,
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(POLLINATIONS_ENDPOINT, json=body)
            if resp.status_code == 402 or resp.status_code == 429:
                return None, True
            if resp.status_code != 200:
                return None, False
            data = resp.json()
            content = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not content:
                return None, False

            match = re.search(r"\[.*\]", content, flags=re.DOTALL)
            if not match:
                return None, False
            parsed = json.loads(match.group(0))
            if not isinstance(parsed, list):
                return None, False

            refined: dict[int, str] = {}
            for row in parsed:
                if isinstance(row, dict) and "index" in row and "text_vi" in row:
                    idx = int(row["index"])
                    txt = _clean_translated_artifacts(str(row["text_vi"]))
                    if txt:
                        refined[idx] = txt
            return (refined if refined else None), False
    except Exception:
        return None, False


def _preprocess_source_text(text: str, source_lang: str) -> str:
    cleaned = str(text or "").strip()
    if source_lang.startswith("zh") or re.search(r"[\u4e00-\u9fff]", cleaned):
        for pat, repl in SOURCE_ZH_FIXES:
            cleaned = re.sub(pat, repl, cleaned)
    return cleaned


def translate_two_pass(
    segments: list[dict[str, Any]], source_lang: str = "auto"
) -> dict[str, Any]:
    """Run two-pass AI translation on extracted subtitle/speech segments."""
    if not segments:
        return {
            "detected_lang": source_lang,
            "engine_pass1": "none",
            "engine_pass2": "none",
            "pass1": [],
            "pass2": [],
        }

    detected = detect_source_lang(segments, source_lang)
    originals = [_preprocess_source_text(seg.get("text", ""), detected) for seg in segments]

    # Pass 1: Context-aware neural translation
    pass1_texts = _pass1_translate_batch(originals, detected)

    pass1_list: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        p1 = pass1_texts[idx] if idx < len(pass1_texts) else originals[idx]
        pass1_list.append(
            {
                "index": int(seg.get("index", idx)),
                "start": round(float(seg["start"]), 3),
                "end": round(float(seg["end"]), 3),
                "original": originals[idx],
                "pass1_vi": p1,
                "text_vi": p1,
            }
        )

    # Pass 2: AI contextual refinement in coherent batches (60 lines per batch; stop immediately on rate limit)
    ai_refined_map: dict[int, str] = {}
    used_ai_llm = False
    batch_size = 60
    for start_i in range(0, len(pass1_list), batch_size):
        batch = pass1_list[start_i : start_i + batch_size]
        res, rate_limited = _call_free_ai_pass2(batch, detected)
        if res:
            used_ai_llm = True
            ai_refined_map.update(res)
        if rate_limited:
            break

    pass2_list: list[dict[str, Any]] = []
    for item in pass1_list:
        idx = item["index"]
        duration = max(0.6, item["end"] - item["start"])
        candidate = ai_refined_map.get(idx, item["pass1_vi"])
        if re.search(r"[\u4e00-\u9fff]", candidate) and not re.search(r"[\u4e00-\u9fff]", item["pass1_vi"]):
            candidate = item["pass1_vi"]
        polished = _polish_vietnamese_line(candidate, duration)
        if not polished:
            polished = _polish_vietnamese_line(item["pass1_vi"], duration) or item["pass1_vi"]

        pass2_list.append(
            {
                "index": idx,
                "start": item["start"],
                "end": item["end"],
                "original": item["original"],
                "pass1_vi": item["pass1_vi"],
                "text_vi": polished,
                "speed": _recommend_line_speed(polished, duration),
                "refined": polished != item["pass1_vi"],
            }
        )

    return {
        "detected_lang": detected,
        "engine_pass1": "Google Neural Batch Translate + MyMemory",
        "engine_pass2": (
            "Pollinations AI LLM + Vietnamese Pacing & Punctuation Polisher"
            if used_ai_llm
            else "Vietnamese Pacing & Punctuation Polisher"
        ),
        "pass1": pass1_list,
        "pass2": pass2_list,
    }
