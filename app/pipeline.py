"""The video pipeline. Every stage saves its result to the job folder, so a job can stop
at any moment (Mac turned off, error, pause) and continue from where it was."""
import json
import re
import shutil
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import animate, balance, captions, db, effects, images, llm, media, music as musiclib, thumb, tts
from .config import JOBS_DIR, MUSIC_DIR, PROMPTS_DIR, PROMPTS_DEFAULT_DIR, load_settings
from .net import Cancelled, UserError

# (id, Russian name, progress weight)
STAGES = [
    ("idea", "Выбор темы", 3),
    ("outline", "Структура и хуки", 4),
    ("script", "Текст сценария", 6),
    ("review", "Проверка редактором", 8),
    # The owner's check comes right after the editor: nothing else is spent before the text is approved.
    ("approval", "Ваша проверка сценария", 0),
    ("scenes", "Сцены и описания картинок", 4),
    ("extras", "Перевод и описание для YouTube", 4),
    ("short_script", "Сценарий шортса", 3),
    ("voice", "Озвучка", 14),
    ("images", "Картинки", 20),
    ("animate", "Анимация сцен", 8),
    ("video", "Сборка видео", 28),
    ("short_video", "Сборка шортса", 6),
]
STAGE_IDS = [s[0] for s in STAGES]
STAGE_NAMES = {s[0]: s[1] for s in STAGES}

CRITERIA_RU = {
    "hook": "Хук (первые 15 секунд)",
    "open_loops": "Открытые вопросы (интрига)",
    "rehooks": "Удерживающие крючки каждые 1–1,5 мин",
    "pacing": "Темп, нет провисаний",
    "clarity": "Понятность",
    "emotion": "Эмоции и ставки",
    "payoff": "Сильная концовка",
    "surprise": "Неожиданность (поворот)",
    "spoken": "Звучит естественно вслух",
    "accuracy": "Достоверность фактов",
    "length": "Длина",
    "voice": "Манера речи героя",
}

WORDS_PER_MIN = 150
WPM = {"en": 150, "ru": 125}  # Russian words are longer, so fewer of them fit in a minute

LANG_OVERRIDE = {
    "ru": """

LANGUAGE OVERRIDE (this rule beats every instruction above and below about language and audience):
- This channel is Russian-language. The audience is Russian-speaking viewers in Russia and the CIS, not US/UK viewers.
- Write every spoken or published text in natural, lively spoken Russian: ideas and titles, hooks, the script, the Short, YouTube titles, descriptions, tags, hashtags and thumbnail text. Never write them in English.
- Write numbers, dates and abbreviations in words the way a Russian narrator would say them when it helps the voice actor.
- Image prompts (fields named prompt and thumbnail_prompt) stay in English, because the image model understands English best.
- Fields ending in _ru simply repeat the same Russian text. Exception: script_ru must be an empty string (the script is already in Russian).
- The Short is 90-115 Russian words; its last line sends viewers to the full story on the channel in Russian.""",
}


class StopForReview(Exception):
    pass


def job_dir(jid: str) -> Path:
    return JOBS_DIR / jid


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


def load_prompt(name: str) -> str:
    p = PROMPTS_DIR / name
    if not p.exists():
        p = PROMPTS_DEFAULT_DIR / name
    return p.read_text("utf-8")


def fill(template: str, **values) -> str:
    for k, v in values.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def word_count(text: str) -> int:
    return len(re.findall(r"[0-9A-Za-zА-Яа-яЁё]+(?:['’-][0-9A-Za-zА-Яа-яЁё]+)*", text))


def speaker_of(para: str, speakers: dict) -> tuple:
    """A paragraph like "Маша: Папа, смотри!" is a line of that character. Returns (character id or "", text)."""
    m = re.match(r"^\s*[*_]*([^:\n]{1,40}?)[*_]*\s*:\s*(.+)$", para, re.S)
    if m and m.group(1).strip().lower() in speakers:
        return speakers[m.group(1).strip().lower()], m.group(2).strip().strip("«»\"“”")
    return "", para


def split_chunks(script: str, target_words: int, speakers: dict = None) -> list:
    """Split the narration into scenes of roughly target_words words, on sentence boundaries.
    speakers: {lowercase name: character id}; their lines become separate scenes voiced by that character."""
    lo, hi = max(int(target_words * 0.6), 4), int(target_words * 1.45)
    chunks = []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", script.strip()) if p.strip()]
    # A character's line can also be a single line inside a paragraph: give every such line its own paragraph.
    if speakers:
        split = []
        for para in paragraphs:
            lines = [l.strip() for l in para.split("\n") if l.strip()]
            if any(speaker_of(l, speakers)[0] for l in lines):
                split += lines
            else:
                split.append(para)
        paragraphs = split
    for para in paragraphs:
        who, para = speaker_of(para, speakers or {})
        para = re.sub(r"\s+", " ", para)
        sentences = re.split(r"(?:(?<=[.!?…])|(?<=[.!?…][\"”’')\]»])|(?<=[.!?…][\"”’')\]»]{2}))\s+(?=[A-ZА-ЯЁ0-9\"“‘'(«—])", para)
        pieces = []
        for s in sentences:
            if word_count(s) > hi:  # long sentence: split at commas/semicolons/dashes
                parts = re.split(r"(?<=[,;:—–])\s+", s)
                buf = ""
                for part in parts:
                    if buf and word_count(buf + " " + part) > target_words:
                        pieces.append(buf)
                        buf = part
                    else:
                        buf = (buf + " " + part).strip()
                if buf:
                    pieces.append(buf)
            else:
                pieces.append(s)
        cur = ""
        para_chunks = []
        for piece in pieces:
            if cur and word_count(cur) >= lo and word_count(cur + " " + piece) > target_words:
                para_chunks.append(cur)
                cur = piece
            else:
                cur = (cur + " " + piece).strip()
        if cur:
            # A tiny tail joins the previous chunk of the same paragraph.
            if para_chunks and word_count(cur) < lo / 2 and word_count(para_chunks[-1] + " " + cur) <= hi:
                para_chunks[-1] += " " + cur
            else:
                para_chunks.append(cur)
        for i, c in enumerate(para_chunks):
            chunks.append({"text": c, "paragraph_end": i == len(para_chunks) - 1, **({"speaker": who} if who else {})})
    return chunks


SHORT_OVERRIDE = """

FORMAT OVERRIDE (this rule beats every instruction above and below about length, structure and pacing):
- This video is a vertical YouTube Short of about {seconds} seconds (about {words} spoken words), not a long video. Ignore the long-video sections, the 35-45-word hook and re-hooks every 60-90 seconds.

HOW A SHORT IS WATCHED (build everything around this):
- The viewer decides in the first 1-2 seconds whether to swipe. They stay only while they cannot predict what happens next. The moment the ending becomes obvious, they leave.
- The deadliest mistake is the predictable story: "a problem appears, the hero solves it the obvious way, everyone is happy". If the viewer can guess the ending from the first sentence, the idea is rejected, however neat it is.
- If the hero is a product, mascot or brand character, the hero simply winning is an advert, and nobody watches adverts. The hero must be underestimated, mocked, fail first, face a rival or a worse problem, or win in an absurd, unexpected way that costs something. Comedy and character beat demonstration.

SHORT STORY ENGINE (every idea, outline and script must have all four):
1) HOOK, the first sentence (2 seconds): drop the viewer into the most extreme moment, already in motion. Strong hook types: an absurd or impossible situation ("My owner tried to shave with a cheese grater."); a conflict mid-fight; a bold claim that sounds wrong; a ticking clock with high or ridiculous stakes; a forbidden thing or a secret; a "this went horribly wrong" opening; a direct challenge to the viewer. Never start with background, a name introduction, the weather or "one day".
2) ESCALATION (to about 40%): things get worse in a way the viewer did not expect. The obvious solution fails or is blocked. Raise the stakes with a concrete complication.
3) TWIST (about 70-80%): a turn nobody saw coming that changes the meaning of what happened: a reveal, a role reversal, an ironic consequence, an absurd solution, the "villain" was right, the hero's win backfires. The twist must be set up by an earlier detail so it feels clever, not random.
4) PUNCHLINE (the last 1-2 sentences): a sharp, funny or bitter final line that rewards the viewer. Ideally it loops into the first sentence so the replay feels seamless. No moral lecture, no summary, no "and so we learned".

PROVEN SHORT FORMATS (pick the one that fits the idea, or mix):
- The insider and the believer: a naive character trusts what everyone is told, a cunning, confident character knows how it really works and reveals it ("what nobody tells you about..."). The hook names a familiar thing and promises the hidden truth; the twist is the reveal; the punchline is the believer's reaction or the insider's cynical last word. The same duo in every video builds a fan base.
- The bet or duel: two characters bet on who is right; the obvious favourite loses or wins in a way nobody expected.
- The disaster: something goes spectacularly wrong from the first second; the story is how it got worse and how it ended absurdly.
- The secret: the hero hides something or discovers something hidden; the last line reveals it.

RETENTION RULES:
- Every sentence either answers the previous question while opening a new one, or raises the stakes. Cut any sentence that only explains.
- Show conflict between characters: they want different things, argue, bet, sabotage. Dialogue lines with personality beat narration.
- Concrete beats abstract: one specific object, number, place or reaction is worth a paragraph of description.
- One situation, a story you could retell in one plain sentence that ends with "...and then it turned out that...".
- Never give away the answer in the hook: open the question, then delay the real answer to the middle (first the common belief or the wrong answer, then "but actually"). An answer in the third sentence kills the video.
- Never promise what the video does not deliver ("there is a simple trick" must be followed by a trick the viewer can really do).
- A host or narrator character does not introduce himself at length; at most a few words woven into the story, never a separate "I am ..." sentence near the start.
- Every comparison must be exact and familiar (noise-cancelling headphones, not "like a vacuum cleaner"); a wrong analogy confuses more than it explains.
- No clichés the audience has seen a thousand times. Never repeat a situation, setting or twist from this channel's previous videos.

WRITING:
- Tell it like a good storyteller to a friend: complete, connected sentences of natural length (mostly 6-15 words) that follow each other ("so", "but", "and then", "because"). Energy comes from the situation and the jokes, not from chopping sentences.
- Never write telegraphic fragments, noun lists or teaser filler ("Not the strangest part.", "The verdict?") unless a real surprise follows immediately.
- No subscribe begging in the middle; one short natural line inviting to subscribe at the very end, after the punchline, in the narrator's voice.
- Scenes change every 3-5 seconds, so image prompts must be bold and readable on a phone: one clear subject, close framing, strong contrast.
- Title: max 60 characters, curiosity-driven, never reveals the twist. Tags and description are for Shorts."""


DEFAULT_SHORT_EXAMPLES = {
    "ru": """Пример 1.
Попробуй пощекотать себя прямо сейчас. Не выходит? И не выйдет, как ни старайся.
Обычно думают, что всё дело в том, что ты знаешь, где будет касание. Почти так, но всё хитрее. Когда ты двигаешь рукой, мозжечок заранее просчитывает, что почувствует кожа, и вычитает это ощущение. Так наушники с шумоподавлением гасят гул самолёта. Поэтому своё касание мозг почти не замечает.
Учёные в Лондоне проверили это на роботе. Человек сам двигал рычаг, а робот щекотал его с задержкой в пятую долю секунды. И стало щекотно! Мозг просто не узнал собственное движение.
Выходит, щекотно только тогда, когда мозг не может угадать. Подписывайся, я знаю ещё много того, о чём тебе не рассказывали.

Пример 2.
Почему в самолёте все вдруг заказывают томатный сок? На земле его почти никто не пьёт, а в небе авиакомпании закупают его тоннами.
Кажется, что это просто привычка. Но дело в твоём языке. В салоне сухой воздух и пониженное давление, и сладкое и солёное мы чувствуем заметно слабее. Еда, которая дома казалась нормальной, на высоте становится пресной.
А вот насыщенный вкус помидоров почти не страдает. Более того, исследователи выяснили, что гул двигателей делает его даже ярче. Поэтому томатный сок в самолёте вкуснее, чем дома.
Подписывайся, в следующий раз расскажу, почему еду в самолёте так сильно солят.

Пример 3.
Говорят, у золотой рыбки память всего три секунды. Поэтому ей якобы не скучно в круглом аквариуме: каждый круг для неё как первый.
Звучит мило, но это выдумка. Рыбок приучали приплывать на звук перед кормёжкой, и они узнавали этот сигнал спустя месяцы. Они помнят, кто их кормит, и могут научиться толкать рычаг ради еды.
Так что, если твоя рыбка бросается к стеклу, когда ты входишь в комнату, это не случайность. Она тебя узнала. И, возможно, запомнила, что вчера ты забыл её покормить.
Подписывайся, дальше будет ещё интереснее.""",
    "en": """Example 1.
Try to tickle yourself right now. Nothing? And there never will be, no matter how hard you try.
Most people think it is because you know where the touch is coming. That is close, but the real reason is sneakier. Every time you move your hand, your cerebellum predicts exactly what your skin will feel and subtracts it, the way noise-cancelling headphones erase the roar of a plane. So your own touch barely registers.
Scientists in London tested this with a robot. People moved a lever themselves, and the robot tickled them with a delay of a fifth of a second. Suddenly it tickled! The brain simply failed to recognise its own movement.
So tickling only works when your brain cannot guess what comes next. Subscribe, there is a lot more nobody told you.

Example 2.
Why does everyone order tomato juice on a plane? Almost nobody drinks it on the ground, yet airlines buy it by the ton.
It looks like a habit, but the reason is your tongue. Cabin air is dry and the pressure is low, so sweet and salty tastes fade noticeably. Food that tastes fine at home turns bland at thirty thousand feet.
The rich savoury taste of tomatoes barely suffers, and researchers found that engine noise even makes it stronger. That is why tomato juice tastes better on a plane than at home.
Subscribe, next time I will tell you why airline food is so salty.""",
}


KIDS_OVERRIDE = """

AUDIENCE OVERRIDE (this rule beats every instruction above and below about truthfulness, tone and dark themes):
- This is a channel for young children (3-8 years old) and their parents. The stories are original, made-up tales, so the "real stories only" rule does not apply; never present them as true events.
- Tone: warm, kind, playful and safe. No violence, no scary moments, no death, no meanness that is not resolved kindly, nothing a parent would object to. Tension comes from curiosity, small problems and friendly surprises.
- Language: very simple words and short sentences a small child understands; gentle repetition and sound words are welcome.
- Every story has a clear, kind lesson (friendship, honesty, courage, sharing, curiosity) shown through actions, plus a happy ending.
- Hooks and re-hooks are friendly questions and surprises ("What was inside the little box?"), never fear or shock.
- Images: cute, bright, friendly; no frightening creatures, no weapons, no danger."""


FICTION_OVERRIDE = """

STORY OVERRIDE (this rule beats every instruction above and below about truthfulness and real stories):
- This channel tells original, made-up animated stories for adults. Invented plots, characters and dialogue are the point, so the "real stories only" rule does not apply. Never present them as true events and never use the names of real private people.
- Themes: the evil of the modern world shown honestly: deception, scams, greed, corruption, manipulation, hypocrisy, cruelty of systems, loneliness, the price of a choice; or simply unusual human stories. Real-world problems may inspire the plot, but satire targets types and systems, not real named people.
- Tone: sharp, grown-up and emotionally honest: dark satire, drama or a bitter parable. Evil is shown clearly and has consequences or a meaning; it is never glorified.
- Limits: no graphic gore or torture, no sexual content, no hate against protected groups, no step-by-step instructions for crimes or scams. Show the darkness through situations, faces and consequences, not explicit detail.
- Storytelling: a clear hero with a goal, rising stakes, at least one strong twist, and an ending that hits: an ironic reversal, a bitter truth or an earned moral (said in one line, not a lecture). Short lines of dialogue inside the narration are welcome ("'Sign here,' he smiled.").
- Images: adult animation, expressive characters and faces, cinematic framing; menace is shown through mood and light, never gore."""


SPOKEN_RULES = """

SPOKEN TEXT ONLY (very important): every word of the script is read aloud by a voice; the viewer may not even look at the screen.
- Never write stage directions, camera or scene descriptions, sound effects or on-screen text ("Close-up.", "The timer beeps.", "Red percent blinking."). What only needs to be seen goes into the image prompts, not the script.
- The narration is full, natural spoken sentences that make sense by ear alone, never telegraphic fragments or lists of nouns.
- Clarity first: within the first 10 seconds the listener knows who the hero is, what he wants and what is at stake. Every other character is introduced on first mention (who or what they are), and at the end the point of the video is clear (the result and the takeaway in one line).
- Keep the story simple enough for its length: in a Short, one hero, one goal, one obstacle, one payoff.
- The recurring characters act and talk in character: their lines show their personality (opinions, jokes, reactions to what happens), they never just list facts or announce ad slogans. Selling points of a product come through the story (what the hero can do that others cannot), not through claims.
- Every sentence follows logically from the one before it; a listener who hears the video once can retell what happened and why."""


SERIES_RULES = """
- This video is EPISODE {num} of an ongoing animated series. Keep the world, the rules and the characters consistent with the series bible and with everything that already happened. Never contradict earlier episodes; characters remember what they lived through and change step by step.
- Every episode must still work for a new viewer: one complete story of its own (its own goal, conflict and resolution) inside the bigger season story.
- {recap}
- End the episode with a strong hook for the next one: an unresolved question, a reveal or a threat, said in the last 1-2 sentences. Do not resolve the big season mystery early.
- Title: you may add "Episode {num}" only at the end of the title if it fits; the title must still be a strong click on its own."""


def script_checks(script: str, target_words: int, wpm: int = WORDS_PER_MIN) -> dict:
    """Automatic checks that don't need a model."""
    wc = word_count(script)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", script.replace("\n", " ")) if s.strip()]
    longest = max((word_count(s) for s in sentences), default=0)
    lower = script.lower()[:600]
    bad_openers = [p for p in ("welcome back", "hey guys", "in this video", "hello everyone", "welcome to",
                               "всем привет", "привет всем", "в этом видео", "добро пожаловать", "с вами снова")
                   if p in lower]
    long_sentences = sum(1 for s in sentences if word_count(s) > 30)
    # Chopped "telegraph" text (many 2-4 word sentences) is hard to follow by ear.
    avg = round(wc / len(sentences), 1) if sentences else 0
    return {
        "avg_sentence": avg,
        "choppy": bool(sentences) and avg < 6.5,
        "word_count": wc,
        "minutes": round(wc / wpm, 1),
        "target_words": target_words,
        "length_ok": abs(wc - target_words) <= target_words * 0.15,
        "longest_sentence": longest,
        "long_sentences": long_sentences,
        "first_sentence": sentences[0] if sentences else "",
        "bad_openers": bad_openers,
    }


# ---------------------------------------------------------------- JSON schemas

SCHEMA_IDEAS = {
    "type": "object",
    "properties": {
        "ideas": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "title_ru": {"type": "string"},
            "premise": {"type": "string"}, "premise_ru": {"type": "string"},
            "hook_angle": {"type": "string"},
            "first_line": {"type": "string"}, "twist": {"type": "string"}, "twist_ru": {"type": "string"},
            "scores": {"type": "object", "properties": {
                "curiosity": {"type": "integer"}, "emotion": {"type": "integer"},
                "clarity": {"type": "integer"}, "uniqueness": {"type": "integer"},
                "visual": {"type": "integer"}}},
        }}},
        "chosen_index": {"type": "integer"},
        "reason_ru": {"type": "string"},
    },
}

SCHEMA_IDEA_CRITIC = {
    "type": "object",
    "properties": {
        "ratings": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "stop_scroll": {"type": "integer"}, "unpredictable": {"type": "integer"},
            "payoff": {"type": "integer"}, "comment_ru": {"type": "string"}}}},
        "chosen_index": {"type": "integer"},
        "reason_ru": {"type": "string"},
    },
}

SHORT_IDEAS_EXTRA = """

SHORTS: for every idea also give first_line (the exact first spoken sentence, in the video's language), twist (the unexpected turn in one sentence) and twist_ru (the same in Russian). An idea without a real twist, or whose ending can be guessed from the first line, is worthless: replace it. Make the ideas different in situation, conflict and type of twist (reveal, role reversal, ironic backfire, absurd solution, rival, failure that turns into a win)."""

IDEA_CRITIC = """You are a jaded 20-year-old who scrolls YouTube Shorts for hours and swipes away from anything boring, predictable or advert-like within 2 seconds. You are judging ideas for one channel.

For every idea score from 1 to 10, honestly and harshly:
- stop_scroll: would the first line make you stop swiping?
- unpredictable: after the first line, could you guess the ending? (10 = no way, 1 = obvious)
- payoff: would the twist and the last line make you laugh, gasp or replay?
Ideas that read like an advert or where the hero simply wins the obvious way get at most 3 for unpredictable.
Add comment_ru: one short honest sentence in Russian.
Then choose the idea you would really watch to the end (chosen_index, counting from 0) and explain in reason_ru in 2 sentences in Russian.

IDEAS:
{ideas}"""


SCHEMA_OUTLINE = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "hook": {"type": "object", "properties": {
            "text_idea": {"type": "string"}, "big_question": {"type": "string"}, "why_ru": {"type": "string"}}},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "start_time": {"type": "string"}, "purpose_ru": {"type": "string"},
            "beats": {"type": "array", "items": {"type": "string"}},
            "rehook": {"type": "string"}, "approx_words": {"type": "integer"}}}},
        "open_loops": {"type": "array", "items": {"type": "object", "properties": {
            "question": {"type": "string"}, "opened_in": {"type": "string"}, "closed_in": {"type": "string"}}}},
        "characters": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "visual": {"type": "string"}}}},
        "locations": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "visual": {"type": "string"}}}},
        "ending_payoff": {"type": "string"},
        "summary_ru": {"type": "string"},
    },
}

SCHEMA_SCRIPT = {"type": "object", "properties": {"script": {"type": "string"}}}

SCHEMA_REVIEW = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {"type": "object", "properties": {
            "criterion": {"type": "string", "enum": list(CRITERIA_RU.keys())},
            "score": {"type": "integer"}, "comment_ru": {"type": "string"}}}},
        "retell_ru": {"type": "string"},
        "issues_ru": {"type": "array", "items": {"type": "string"}},
        "verdict_ru": {"type": "string"},
        "revised_script": {"type": "string"},
    },
}

SCHEMA_PROMPTS = {
    "type": "object",
    "properties": {"prompts": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "prompt": {"type": "string"}, "prompt_ru": {"type": "string"},
        "location": {"type": "string"},
        "effect": {"type": "string", "enum": effects.EFFECTS}, "sfx": {"type": "string", "enum": effects.SFX},
        "characters": {"type": "array", "items": {"type": "string"}},
        "items": {"type": "array", "items": {"type": "string"}}}}}},
}

SCHEMA_EXTRAS = {
    "type": "object",
    "properties": {
        "script_ru": {"type": "string"},
        "youtube_title": {"type": "string"}, "youtube_title_ru": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "thumbnail_text": {"type": "string"}, "thumbnail_text_ru": {"type": "string"},
        "thumbnail_prompt": {"type": "string"},
        "music_prompt": {"type": "string"}, "music_style_ru": {"type": "string"},
    },
}

SCHEMA_SHORT = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "title_ru": {"type": "string"},
        "script": {"type": "string"}, "script_ru": {"type": "string"},
        "why_ru": {"type": "string"},
        "hook_score": {"type": "integer"}, "payoff_score": {"type": "integer"},
    },
}


# ---------------------------------------------------------------- context

class Ctx:
    def __init__(self, job: dict):
        self.job = job
        self.id = job["id"]
        self.dir = job_dir(self.id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings = load_settings()
        self.opts = job["options"]
        from . import projects
        self.project = projects.get(self.opts.get("project_id", ""))
        # A channel can have its own voice and image style.
        if self.opts.get("channel_id"):
            from . import youtube
            prof = youtube.get_profile(self.opts["channel_id"])
            if prof.get("voice_id"):
                self.settings["elevenlabs_voice_id"] = prof["voice_id"]
                self.settings["elevenlabs_voice_name"] = prof.get("voice_name", "")
            if prof.get("image_style"):
                self.settings["image_style"] = prof["image_style"]
        # A project's own choices win over the channel and the general settings.
        if self.project.get("voice_id"):
            self.settings["elevenlabs_voice_id"] = self.project["voice_id"]
            self.settings["elevenlabs_voice_name"] = self.project.get("voice_name", "")
        if self.project.get("image_style"):
            self.settings["image_style"] = self.project["image_style"]
        if self.project.get("tts_model"):
            self.settings["elevenlabs_model"] = self.project["tts_model"]
        for k in ("voice_stability", "voice_similarity", "voice_style", "voice_speed"):
            if self.project.get(k) not in (None, ""):
                self.settings[k] = float(self.project[k])
        self.lang = self.opts.get("language") or self.project.get("language") or "en"
        self.characters = read_json(self.dir / "characters.json", []) or []
        self.items = read_json(self.dir / "items.json", []) or []
        host = self.host()
        if host:  # the hero tells the whole video in his own voice
            self.settings["elevenlabs_voice_id"] = host["voice_id"]
            self.settings["elevenlabs_voice_name"] = host.get("voice_name", "")
        self.wpm = WPM.get(self.lang, WORDS_PER_MIN)
        self.demo = bool(self.opts.get("demo"))
        self.usage = read_json(self.dir / "usage.json", {}) or {}
        self.short_format = self.opts.get("format") == "short"
        self.vertical = self.short_format
        self.short_seconds = int(self.opts.get("short_seconds") or 50)
        self.minutes = (self.short_seconds / 60 if self.short_format else
                        float(self.opts.get("duration_min") or self.settings["duration_min"]))
        self.words = int(self.minutes * self.wpm) if not self.demo else 80
        self.niche = self.opts.get("niche") or self.settings["niche"]
        self.episode = int(self.opts.get("episode") or 0) if self.project.get("series") else 0
        self._lock = threading.Lock()
        self.stage = ""

    # -- bookkeeping
    def log(self, msg: str):
        with self._lock, open(self.dir / "log.txt", "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")

    def save_usage(self):
        with self._lock:
            write_json(self.dir / "usage.json", self.usage)

    def add_usage(self, key: str, amount: float):
        with self._lock:
            self.usage[key] = round(self.usage.get(key, 0) + amount, 4)

    def check(self):
        j = db.get(self.id)
        # Anything but "running" means the user paused, restarted or deleted the job while this run was going.
        if j is None or j["status"] != "running":
            raise Cancelled()

    def progress(self, frac: float):
        total = sum(w for s, _, w in STAGES if self.stage_enabled(s))
        done = 0
        for s, _, w in STAGES:
            if not self.stage_enabled(s):
                continue
            if s == self.stage:
                done += w * max(0.0, min(frac, 1.0))
                break
            done += w
        db.update(self.id, progress=round(100 * done / max(total, 1), 1), stage=self.stage)

    def stage_enabled(self, s: str) -> bool:
        if s in ("short_script", "short_video") and (self.short_format or not self.opts.get("make_short")):
            return False
        return True

    def char_name(self, ch: dict) -> str:
        return (ch.get("name") if self.lang == "ru" else ch.get("name_en")) or ch.get("name", "")

    def char_block(self) -> str:
        if not self.characters:
            return ""
        lines = "\n".join(f"- {self.char_name(ch)} ({ch.get('role') or 'recurring character'}): "
                          f"{ch.get('personality', '')} Looks: {ch.get('appearance', '')}"
                          + (f" Owner's additions, use them when they fit the scene (may be in Russian): {ch['extra_ru']}"
                             if ch.get("extra_ru") else "")
                          + (f" MANNER OF SPEECH, write every line of this character (the whole narration when this character is the "
                             f"host or narrator) exactly in this manner, with these "
                             f"words and turns of phrase; sample lines show the style, do not copy them word for word "
                             f"(in Russian): {ch['speech_ru']}" if ch.get("speech_ru") else "") for ch in self.characters)
        return ("\n\nRECURRING CHARACTERS OF THIS CHANNEL (the owner chose them for this video):\n" + lines + "\n"
                "- Build the video around these characters and use exactly these names. If the story is fiction "
                "(a tale, a myth, a made-up story), they are its main characters. If the story is real, never insert "
                "them into real events as if they happened: they are the narrator, host or guide who tells the "
                "story, reacts to it and appears in some pictures (introduce them naturally, keep facts true).\n"
                "- In image prompts always describe them with their exact look. Change clothes or details only "
                "where the owner's additions or the story clearly ask for it, and write that change into the prompt.")

    def item_block(self) -> str:
        if not self.items:
            return ""
        lines = "\n".join(f"- {self.char_name(it)}" + (f" ({it['kind']})" if it.get("kind") else "")
                          + f": {it.get('appearance', '')}"
                          + (f" Owner's notes, follow them (may be in Russian): {it['extra_ru']}" if it.get("extra_ru") else "")
                          for it in self.items)
        return ("\n\nITEMS AND PRODUCTS FOR THIS VIDEO (the owner chose them, they have exact reference photos):\n"
                + lines + "\n"
                "- Let these items appear in the story and in several pictures where they fit naturally: used, held, "
                "worn, standing in the scene. Call them by exactly these names; never invent other brands or models "
                "of the same kind.\n"
                "- In image prompts describe every such item with its exact look above; never change its shape, colours, "
                "logo or labels.")

    def host(self):
        """In "host" mode the first chosen character with a voice tells the whole video himself."""
        if self.project.get("voice_mode") != "host":
            return None
        return next((ch for ch in self.characters if ch.get("voice_id")), None)

    def voiced(self) -> list:
        """Characters with their own voice, unless the project wants only the narrator (or one hero tells it all)."""
        if self.project.get("voice_mode") in ("narrator", "host"):
            return []
        return [ch for ch in self.characters if ch.get("voice_id")]

    def speakers(self) -> dict:
        out = {}
        for ch in self.voiced():
            for n in (ch.get("name"), ch.get("name_en")):
                if n:
                    out[n.strip().lower()] = ch["id"]
        return out

    def dialogue_block(self) -> str:
        host = self.host()
        if host:
            name = self.char_name(host)
            return ("\n\nHOST: the whole video is told by " + name + " in the first person, straight to the "
                    "viewer, in this character's own voice and manner (see CHARACTERS). It is a monologue: the host "
                    "explains, reacts, jokes and tells what happens around in their own words (\"And then the foam guy splashes me, "
                    "can you believe it?\"). No separate narrator, no \"" + name + ":\" prefixes, no quotes around "
                    "the host's words. Other characters do not speak with their own voices: the host retells what they say."
                    + SPOKEN_RULES)
        voiced = self.voiced()
        if not voiced:
            return SPOKEN_RULES if (self.project.get("fiction") or self.characters) else ""
        names = ", ".join(self.char_name(ch) for ch in voiced)
        heavy = self.project.get("voice_mode") == "dialogue"
        return ("\n\nCHARACTER VOICES: " + names + " have their own voices in this video, so they really speak. "
                + ("This is a cartoon: tell the story mostly through their dialogue; the narrator only links scenes "
                   "in short lines. " if heavy else
                   "Let them speak in short, natural lines at the key moments (reactions, questions, jokes, "
                   "arguments, the punchline); the narrator tells the rest. ")
                + "FORMAT (very important, keep it in every rewrite): every line a character says is its own "
                "paragraph that starts with the character's exact name and a colon, for example:\n"
                + "\n".join(f"{self.char_name(ch)}: <what {self.char_name(ch)} says>" for ch in voiced[:2])
                + "\nNo quotes around the line, no stage directions inside it, no \"he said\" after it. "
                "Everything that is not such a paragraph is read by the narrator. Lines must sound like each "
                "character (a child speaks like a child). Other people in the story have no voice: the narrator "
                "retells what they say." + SPOKEN_RULES)

    def series_block(self) -> str:
        if not self.episode:
            return ""
        from . import projects
        prev = [e for e in projects.list_episodes(self.project["id"]) if e.get("num", 0) < self.episode]
        parts = ["\n\nSERIES (this video is part of a series the owner is building):"]
        if self.project.get("series_bible"):
            parts.append("SERIES BIBLE written by the owner (may be in Russian), follow it:\n"
                         + self.project["series_bible"].strip())
        if prev:
            older, recent = prev[:-5], prev[-5:]
            if older:
                parts.append("EARLIER EPISODES (short):\n" + "\n".join(
                    f"- Episode {e['num']}: {e.get('title', '')}. {e.get('logline', '')}" for e in older))
            parts.append("PREVIOUS EPISODES (what already happened, in order):\n" + "\n".join(
                f"- Episode {e['num']} \"{e.get('title', '')}\": {e.get('summary', '')}"
                + (f" Unresolved: {'; '.join(e['open_threads'])}." if e.get("open_threads") else "")
                + (f" Ended on: {e['cliffhanger']}" if e.get("cliffhanger") else "") for e in recent))
            recap = ("After the hook (not before it), give a very short recap of the previous episode in 1-2 "
                     "sentences, then pick up its ending: answer or develop the hook it left.")
        else:
            recap = ("This is the first episode: introduce the hero, the world and the main conflict of the season "
                     "quickly through action, not exposition.")
        plan = projects.plan_line(self.project, self.episode)
        if plan:
            parts.append(f"OWNER'S PLAN FOR THIS EPISODE (may be in Russian): {plan}")
        parts.append(SERIES_RULES.format(num=self.episode, recap=recap).strip())
        return "\n\n".join(parts)

    def history_block(self) -> str:
        """Ideas of this project's earlier videos, so new ones do not repeat them."""
        pid = self.project.get("id")
        if not pid:
            return ""
        seen = []
        for j in sorted(db.list_all(), key=lambda j: j["id"], reverse=True):
            if j["id"] == self.id or (j.get("options") or {}).get("project_id") != pid:
                continue
            idea = read_json(job_dir(j["id"]) / "idea.json", {}) or {}
            try:
                ch = idea["ideas"][idea["chosen_index"]]
            except (KeyError, IndexError, TypeError):
                continue
            seen.append(f"- {ch.get('title', '')}: {ch.get('premise', '')[:220]}")
            if len(seen) >= 25:
                break
        if not seen:
            return ""
        return ("\n\nPREVIOUS VIDEOS OF THIS CHANNEL (newest first). New ideas must be clearly different: "
                "no repeated situations, settings, problems, solutions or twists:\n" + "\n".join(seen))

    def examples_block(self) -> str:
        """Shorts the owner considers excellent (pasted in the project): a style reference, not material to copy."""
        if not self.short_format:
            return ""
        ex = (self.project.get("short_examples") or "").strip()
        if not ex:
            return ("\n\nEXAMPLES OF THE REQUIRED WRITING LEVEL (only the quality bar: how sentences connect, how the "
                    "answer is delayed, how exact the comparison is, how the ending lands; never reuse their topics, "
                    "facts or jokes, and follow this channel's own niche and characters):\n<<<\n"
                    + DEFAULT_SHORT_EXAMPLES.get(self.lang, DEFAULT_SHORT_EXAMPLES["en"]) + "\n>>>")
        return ("\n\nREFERENCE SHORTS THE OWNER LOVES (texts or descriptions of viral Shorts, may be in Russian). "
                "Study how they hook in the first second, how they escalate, where the twist comes and how the "
                "last line lands, and reach the same level. Never copy their plots, jokes or lines:\n<<<\n"
                + ex[:6000] + "\n>>>")

    def golden_block(self, prompt_file: str) -> str:
        """Scripts of this project that the owner marked as successful: the bar for ideas and writing."""
        if prompt_file not in GOLDEN_STAGES or not self.project.get("id"):
            return ""
        from . import projects
        kind = "short" if (self.short_format or prompt_file == "07_short.txt") else "video"
        items = [x for x in projects.list_golden(self.project["id"]) if x.get("kind") == kind][-GOLDEN_IN_PROMPT:]
        if not items:
            return ""
        limit = 2500 if kind == "short" else 1500  # long videos: the title and the opening, where viewers stay or leave
        parts = []
        for x in reversed(items):
            head = f"### {x.get('title', '')}" + (f" ({x['views']} views)" if x.get("views") else "")
            note = f"\nWhy the owner likes it (Russian): {x['note_ru']}" if x.get("note_ru") else ""
            parts.append(f"{head}{note}\n{x.get('text', '')[:limit]}")
        return ("\n\nSUCCESSFUL VIDEOS OF THIS CHANNEL (the owner marked them as what works here; newest first"
                + ("" if kind == "short" else ", openings only") + "). Study why they work: the kind of topic, the hook, "
                "the rhythm, how the story turns, the voice. Aim for the same level and the same audience; never repeat "
                "their topics, plots or lines:\n<<<\n" + "\n\n".join(parts) + "\n>>>")

    def lessons_block(self) -> str:
        """Rules learned from this project's own YouTube numbers (see analytics.py)."""
        if not self.project.get("id"):
            return ""
        from . import analytics
        rules = analytics.active_rules(self.project["id"])
        if not rules:
            return ""
        return ("\n\nWHAT WORKS ON THIS CHANNEL (learned from the statistics of the owner's own published videos; "
                "follow these rules unless the owner's topic or the format rules above clearly say otherwise):\n"
                + "\n".join(f"- {r['rule']}" for r in rules))

    def scene_words(self, key: str) -> int:
        """Words per picture, adjusted for the language so a picture stays on screen as long."""
        return max(4, round(int(self.settings[key]) * self.wpm / WORDS_PER_MIN))

    def out(self, stage: str) -> Path:
        return self.dir / f"{stage}.json"

    def data(self, stage: str):
        return read_json(self.out(stage))

    def voice_rule(self, prompt_file: str) -> str:
        """The owner described how a character talks: the writer and the editor must keep that voice, not a
        neutral narrator's. Repeated at the end of the task, where the model pays the most attention."""
        talk = [ch for ch in self.characters if (ch.get("speech_ru") or "").strip()]
        if not talk or prompt_file not in VOICE_STAGES:
            return ""
        who = "\n".join(f"- {self.char_name(ch)}: {ch['speech_ru'].strip()}" for ch in talk)
        text = ("\n\nVOICE OF THE CHARACTER (most important style rule, the owner wrote it in Russian):\n" + who + "\n"
                "Every line this character says, and the whole narration if this character is the host or narrator, "
                "must sound unmistakably like him: his tone, his pet words and turns of phrase, his way of addressing "
                "the viewer, his humour. Use several of his pet words in every script and build sentences the way he "
                "talks. A neutral, polite or textbook narrator voice is a failure. Do not copy the sample lines word "
                "for word; write new lines in the same manner. His slang is allowed and wanted, but no hard swearing.")
        if prompt_file == "04_review.txt":
            text += ("\nAdd the criterion voice: does the script sound like this character (10) or like a neutral "
                     "narrator (at most 4)? Below 7 means revised_script must be rewritten in his voice. "
                     "Never smooth his slang or pet words out when you polish.")
        return text

    def ask(self, prompt_file: str, schema: dict, max_tokens: int = 32000, extra: str = "", **values) -> dict:
        system = (fill(load_prompt("00_system.txt"), niche=self.niche) + LANG_OVERRIDE.get(self.lang, "")
                  + (SHORT_OVERRIDE.format(seconds=self.short_seconds, words=self.words) if self.short_format else "")
                  + (KIDS_OVERRIDE if self.project.get("made_for_kids")
                     else FICTION_OVERRIDE if self.project.get("fiction") else "")
                  + self.char_block() + self.item_block() + self.dialogue_block() + self.series_block() + self.examples_block()
                  + self.lessons_block() + self.golden_block(prompt_file))
        user = fill(load_prompt(prompt_file), niche=self.niche, minutes=max(1, int(self.minutes)), words=self.words,
                    words_min=int(self.words * 0.9), words_max=int(self.words * 1.1), **values) + extra \
            + self.voice_rule(prompt_file)
        result = llm.call_json(self.settings, system, user, schema, self.usage, max_tokens)
        self.save_usage()
        return result


# ---------------------------------------------------------------- stages

GOLDEN_STAGES = ("01_ideas.txt", "02_outline.txt", "03_script.txt", "04_review.txt", "07_short.txt")
GOLDEN_IN_PROMPT = 6
VOICE_STAGES = ("02_outline.txt", "03_script.txt", "04_review.txt", "07_short.txt")


def ideas_count(settings: dict) -> int:
    try:
        return max(3, min(int(float(settings.get("ideas_count") or 5)), 10))
    except (TypeError, ValueError):
        return 5


def _idea_block(c: Ctx) -> str:
    topic = (c.job.get("topic") or "").strip()
    if not topic and c.episode:
        from . import projects
        topic = projects.plan_line(c.project, c.episode)
    if c.episode:
        block = (f"This is episode {c.episode} of the series. All ideas are possible plots for this episode that "
                 "continue the series naturally (see SERIES above)"
                 + (f" and follow the owner's plan: \"{topic}\"." if topic else "."))
    elif topic:
        block = (f"The channel owner suggested this topic (it may be written in Russian):\n\"{topic}\"\n"
                 "Stay very close to this topic: every idea must be about exactly what the owner wrote, not a "
                 "neighbouring subject. The first idea is the owner's topic itself, turned into a video as written "
                 "(keep every point the owner named). The other ideas are close variations of it: the same subject "
                 "with a different hook, a different key fact or a different example, so the owner can pick the "
                 "best wording of their own topic.")
    else:
        block = "Choose freely within the channel niche. Make the ideas diverse in era, place and type of story."
    return block + c.history_block()


def _ask_ideas(c: Ctx, n: int, avoid: list = None) -> dict:
    extra = SHORT_IDEAS_EXTRA if c.short_format else ""
    extra += f"\n\nGive exactly {n} ideas, no more and no fewer."
    if (c.job.get("topic") or "").strip() and not c.episode:
        extra += ("\n\nThe owner gave a topic: variety only inside that topic, never leave it for another subject."
                  + (" The owner's topic itself is already in the list, so give new close variations of it." if avoid else ""))
    if avoid:
        extra += ("\n\nThese ideas were already proposed for this video and the owner wants NEW ones: no repeated "
                  "stories, situations or twists:\n" + "\n".join(f"- {t}" for t in avoid))
    return c.ask("01_ideas.txt", SCHEMA_IDEAS, 16000, extra=extra, topic_block=_idea_block(c), count=n)


def st_idea(c: Ctx):
    n = ideas_count(c.settings)
    if c.demo:
        r = _demo("idea")
    else:
        c.log(f"Придумываю {n} тем и выбираю лучшую…")
        r = _ask_ideas(c, n)
    ideas = r.get("ideas") or []
    if not ideas:
        raise UserError("Модель не предложила ни одной темы. Запустите шаг ещё раз.")
    for idea in ideas:
        sc = idea.get("scores") or {}
        idea["total"] = round(sum(sc.values()) / max(len(sc), 1), 1)
    idx = r.get("chosen_index", 0)
    if not isinstance(idx, int) or not 0 <= idx < len(ideas):
        idx = max(range(len(ideas)), key=lambda i: ideas[i]["total"])
    if c.short_format and not c.demo and len(ideas) > 1:
        idx = _critic_pick(c, ideas, r, idx)
    r["chosen_index"] = idx
    chosen = ideas[idx]
    db.update(c.id, title=chosen["title"])
    c.opts["title_ru"] = chosen.get("title_ru", "")
    db.update(c.id, options=c.opts)
    c.log(f"Выбрана тема: {chosen.get('title_ru') or chosen['title']}")
    write_json(c.out("idea"), r)


def more_ideas(jid: str) -> int:
    """The owner asked for more topics: a few new ones are added to the list, nothing else changes."""
    job = db.get(jid)
    d = job_dir(jid)
    r = read_json(d / "idea.json")
    if not job or not r:
        raise UserError("Темы ещё не готовы.")
    c = Ctx(job)
    n = ideas_count(c.settings)
    old = r.get("ideas") or []
    new = (_ask_ideas(c, n, [f"{x.get('title', '')}: {x.get('premise', '')[:160]}" for x in old]).get("ideas") or [])[:n]
    for idea in new:
        sc = idea.get("scores") or {}
        idea["total"] = round(sum(sc.values()) / max(len(sc), 1), 1)
        idea["more"] = True
    r["ideas"] = old + new
    write_json(d / "idea.json", r)
    c.log(f"Добавлено ещё {len(new)} тем по вашей просьбе.")
    return len(new)


def _critic_pick(c: Ctx, ideas: list, r: dict, idx: int) -> int:
    """Shorts: a harsh "viewer" judges the ideas by the first line and the twist and picks the one he would watch."""
    c.log("Строгий зритель выбирает тему, которую досмотрел бы до конца…")
    text = "\n\n".join(f"{i}. {x.get('title', '')}\nFirst line: {x.get('first_line', '')}\n"
                        f"Story: {x.get('premise', '')}\nTwist: {x.get('twist', '')}" for i, x in enumerate(ideas))
    try:
        cr = llm.call_json(c.settings, "You judge YouTube Shorts ideas. Answer only with JSON.",
                           IDEA_CRITIC.format(ideas=text), SCHEMA_IDEA_CRITIC, c.usage, 6000)
        c.save_usage()
    except UserError as e:
        c.log(f"Строгий зритель не ответил ({e}), беру выбор сценариста.")
        return idx
    for x in cr.get("ratings", []):
        i = x.get("index")
        if isinstance(i, int) and 0 <= i < len(ideas):
            ideas[i]["viewer"] = {k: x.get(k) for k in ("stop_scroll", "unpredictable", "payoff", "comment_ru")}
    pick = cr.get("chosen_index")
    if not isinstance(pick, int) or not 0 <= pick < len(ideas):
        rated = [i for i in range(len(ideas)) if ideas[i].get("viewer")]
        if not rated:
            return idx
        pick = max(rated, key=lambda i: sum(int(ideas[i]["viewer"].get(k) or 0)
                                          for k in ("stop_scroll", "unpredictable", "payoff")))
    r["writer_choice"] = idx
    r["reason_ru"] = cr.get("reason_ru") or r.get("reason_ru", "")
    return pick


def _chosen_idea(c: Ctx) -> dict:
    r = c.data("idea")
    return r["ideas"][r["chosen_index"]]


def needs_idea_choice(job: dict) -> bool:
    opts = job.get("options", {})
    return bool(opts.get("idea_pause")) and not opts.get("idea_picked")


def choose_idea(jid: str, index: int):
    """The owner picked a topic by hand: everything after the topic is made again from it."""
    d = job_dir(jid)
    r = read_json(d / "idea.json")
    ideas = (r or {}).get("ideas") or []
    if not 0 <= index < len(ideas):
        raise UserError("Такой темы нет. Обновите страницу.")
    r.setdefault("auto_choice", r.get("chosen_index"))
    r["chosen_index"] = index
    r["picked_by_owner"] = True
    write_json(d / "idea.json", r)
    job = db.get(jid)
    opts = dict(job["options"], idea_picked=True, title_ru=ideas[index].get("title_ru", ""))
    db.update(jid, options=opts, title=ideas[index]["title"])
    reset_from(jid, "outline")


def st_outline(c: Ctx):
    if needs_idea_choice(db.get(c.id) or {"options": c.opts}):
        raise StopForReview("⏸ Темы готовы. Выберите тему в шаге «Выбор темы» и нажмите «Взять эту тему».")
    idea = _chosen_idea(c)
    text = (f"Title: {idea['title']}\nPremise: {idea['premise']}\nHook angle: {idea.get('hook_angle', '')}")
    if idea.get("first_line"):
        text += f"\nFirst line: {idea['first_line']}\nTwist (keep it hidden until late): {idea.get('twist', '')}"
    if c.demo:
        r = _demo("outline")
    else:
        c.log("Строю структуру: хук, повороты, удерживающие крючки…")
        r = c.ask("02_outline.txt", SCHEMA_OUTLINE, 16000, idea=text)
    write_json(c.out("outline"), r)


def _outline_text(c: Ctx) -> str:
    o = c.data("outline")
    lines = [f"TITLE: {o.get('title')}",
             f"HOOK: {o['hook'].get('text_idea')} | Big question: {o['hook'].get('big_question')}"]
    for s in o.get("sections", []):
        lines.append(f"\n[{s.get('start_time')}] {s.get('name')} (~{s.get('approx_words')} words)")
        for b in s.get("beats", []):
            lines.append(f"  - {b}")
        if s.get("rehook"):
            lines.append(f"  Re-hook: {s['rehook']}")
    if o.get("open_loops"):
        lines.append("\nOPEN LOOPS:")
        for l in o["open_loops"]:
            lines.append(f"  - {l.get('question')} (opened: {l.get('opened_in')}, closed: {l.get('closed_in')})")
    lines.append(f"\nENDING PAYOFF: {o.get('ending_payoff')}")
    return "\n".join(lines)


def st_script(c: Ctx):
    if c.demo:
        r = _demo("script")
    else:
        c.log(f"Пишу текст сценария (~{c.words} слов)…")
        r = c.ask("03_script.txt", SCHEMA_SCRIPT, 32000, outline=_outline_text(c))
    script = r["script"].strip()
    r["checks"] = script_checks(script, c.words, c.wpm)
    c.log(f"Текст готов: {r['checks']['word_count']} слов (~{r['checks']['minutes']} мин)")
    write_json(c.out("script"), r)


def _evaluate(c: Ctx, script: str, round_no: int) -> dict:
    if c.demo:
        r = _demo("review", script=script)
    else:
        c.log(f"Редактор: раунд {round_no}, проверяю по чек-листу…")
        r = c.ask("04_review.txt", SCHEMA_REVIEW, 32000, outline=_outline_text(c), script=script,
                  word_count=word_count(script))
    if not c.voice_rule("04_review.txt"):  # voice is judged only when the owner described a manner of speech
        r["scores"] = [s for s in r.get("scores", []) if s.get("criterion") != "voice"]
    scores = {s["criterion"]: s for s in r.get("scores", []) if s.get("criterion") in CRITERIA_RU}
    values = [max(1, min(10, int(s["score"]))) for s in scores.values()] or [0]
    r["overall"] = round(statistics.mean(values), 2)
    r["min_score"] = min(values)
    r["checks"] = script_checks(script, c.words, c.wpm)
    for s in r.get("scores", []):
        s["criterion_ru"] = CRITERIA_RU.get(s.get("criterion"), s.get("criterion"))
    thr = float(c.settings["quality_threshold"])
    min_crit = int(c.settings["min_criterion_score"])
    r["passed"] = r["overall"] >= thr and r["min_score"] >= min_crit and r["checks"]["length_ok"] \
        and not r["checks"]["bad_openers"] and not r["checks"].get("choppy")
    r["evaluated_script"] = script
    return r


def st_review(c: Ctx):
    script = c.data("script")["script"].strip()
    max_rounds = max(1, int(c.settings["max_editor_rounds"]))
    state = read_json(c.dir / "review_progress.json", {"rounds": []})
    rounds = state["rounds"]
    current = rounds[-1]["revised_script"] if rounds else script
    final = None
    while True:
        c.check()
        if rounds and rounds[-1]["passed"]:
            final = rounds[-1]["evaluated_script"]
            break
        if len(rounds) >= max_rounds + 1:
            break
        r = _evaluate(c, current, len(rounds) + 1)
        r["revised_script"] = (r.get("revised_script") or "").strip() or current
        rounds.append(r)
        write_json(c.dir / "review_progress.json", {"rounds": rounds})
        c.log(f"Оценка раунда {len(rounds)}: {r['overall']} из 10 (минимум {r['min_score']}), "
              f"{r['checks']['word_count']} слов — {'принято' if r['passed'] else 'переписываю'}")
        c.progress(len(rounds) / (max_rounds + 1))
        if r["passed"]:
            final = current
            break
        if len(rounds) >= max_rounds:
            # Last allowed rewrite: score it once more so the numbers shown belong to the final text.
            current = r["revised_script"]
            last = _evaluate(c, current, len(rounds) + 1)
            last["revised_script"] = current
            last["score_only"] = True
            rounds.append(last)
            # Pick the best-scored version among all evaluated texts.
            best = max(rounds, key=lambda x: (x["passed"], x["overall"]))
            final = best["evaluated_script"]
            break
        current = r["revised_script"]
    if final is None:
        best = max(rounds, key=lambda x: (x["passed"], x["overall"]))
        final = best["evaluated_script"]
    best = next(x for x in reversed(rounds) if x["evaluated_script"] == final)
    result = {
        "rounds": rounds,
        "final_script": final,
        "final_overall": best["overall"],
        "passed": best["passed"],
        "checks": script_checks(final, c.words, c.wpm),
    }
    if not best["passed"]:
        c.log("Внимание: сценарий не дотянул до порога качества за отведённые раунды. "
              "Взята лучшая версия. Можно запустить шаг ещё раз.")
    write_json(c.out("review"), result)
    (c.dir / "review_progress.json").unlink(missing_ok=True)
    (c.dir / "script_final.txt").write_text(final, "utf-8")


def set_final_script(jid: str, text: str):
    """The owner's own edit of the text to be voiced. Scenes, pictures, voice and video are then made again from it."""
    d = job_dir(jid)
    review = read_json(d / "review.json")
    if not review:
        raise UserError("Текст ещё не готов: дождитесь шага «Проверка редактором».")
    text = re.sub(r"\n{3,}", "\n\n", text.replace("\r", "")).strip()
    if word_count(text) < 5:
        raise UserError("Текст слишком короткий.")
    old = review.get("checks") or {}
    wpm = round(old["word_count"] / old["minutes"]) if old.get("minutes") else WORDS_PER_MIN
    review["final_script"] = text
    review["edited_by_owner"] = True
    review["checks"] = script_checks(text, old.get("target_words", word_count(text)), wpm)
    write_json(d / "review.json", review)
    (d / "script_final.txt").write_text(text, "utf-8")
    reset_from(jid, "scenes")


# Camera plan: neighbouring pictures never share a camera angle, so even one room looks alive.
SHOTS = [
    ("wide", "wide establishing shot showing the whole place and where everyone is"),
    ("medium", "medium shot from the waist up, eye level"),
    ("pov", "first-person point of view through the viewer's own eyes, hands or objects in the foreground"),
    ("closeup", "close-up of a face or of the main character filling the frame, strong emotion"),
    ("ots", "over-the-shoulder shot from behind one character looking at the other person or object"),
    ("detail", "extreme close-up of one telling detail or object"),
    ("low", "low-angle shot from below, the subject looks big and dramatic"),
    ("high", "high-angle or top-down shot looking down at the scene"),
    ("profile", "side view in profile, the character facing left or right of the frame"),
]


def shot_plan(n: int, seed: str) -> list:
    """A varied camera angle for each of n pictures: starts wide, never repeats a neighbour or its neighbour."""
    import random
    rnd = random.Random(seed)
    keys = [k for k, _ in SHOTS]
    plan = []
    for i in range(n):
        if i == 0:
            plan.append("wide")
            continue
        avoid = set(plan[-2:])
        if plan.count("wide") * 6 < i:  # re-establish the place now and then
            pick = "wide" if "wide" not in avoid else None
        else:
            pick = None
        plan.append(pick or rnd.choice([k for k in keys if k not in avoid and k != "wide"]))
    return plan


def _prompts_for(c: Ctx, chunks: list, vertical: bool, title: str) -> list:
    o = c.data("outline") or {}
    saved = [f"- {c.char_name(ch)}: {ch.get('appearance', '')}"
             + (f" (owner's additions when they fit: {ch['extra_ru']})" if ch.get("extra_ru") else "")
             for ch in c.characters]
    chars = "\n".join(saved + [f"- {x['name']}: {x['visual']}" for x in o.get("characters", [])]) or "- (none)"
    locs = "\n".join(f"- {x['name']}: {x['visual']}" for x in o.get("locations", [])) or "- (none)"
    orientation = ("Vertical 9:16 composition: keep the main subject centered, tall framing."
                   if vertical else "Horizontal 16:9 cinematic composition.")
    out = []
    batch = 45
    shots = shot_plan(len(chunks), f"{c.id}-{title}-{vertical}")
    shot_text = dict(SHOTS)
    for start in range(0, len(chunks), batch):
        part = chunks[start:start + batch]
        who = {x["id"]: c.char_name(x) for x in c.characters}
        listing = "\n".join(f"{start + i}. [camera: {shot_text[shots[start + i]]}] "
                            + (f"[{who[ch['speaker']]} says] " if ch.get("speaker") in who else "")
                            + ch["text"] for i, ch in enumerate(part))
        if c.demo:
            demo_fx = [("none", "none"), ("punch_in", "impact"), ("none", "whoosh"), ("shake", "impact"),
                       ("zoom_out", "none")]
            r = {"prompts": [{"index": start + i, "prompt": "demo", "prompt_ru": "демо",
                              "effect": demo_fx[(start + i) % 5][0], "sfx": demo_fx[(start + i) % 5][1]}
                             for i in range(len(part))]}
        else:
            fx = effects.PROMPT_BLOCK.format(limit=effects.LIMIT["short" if vertical else "long"])
            names = ", ".join(c.char_name(ch) for ch in c.characters)
            fx += (f"\n\nIn field characters list which of the channel's recurring characters ({names}) are visible "
                   "in the picture, using exactly these names; otherwise an empty list." if names else
                   "\n\nLeave the field characters as an empty list.")
            inames = ", ".join(c.char_name(it) for it in c.items)
            fx += (f"\n\nIn field items list which of the chosen items ({inames}) are visible in the picture, using "
                   "exactly these names; otherwise an empty list. Show these items in the pictures where the narration "
                   "mentions or uses them, and in a few other fitting scenes, described with their exact look." if inames
                   else "\n\nLeave the field items as an empty list.")
            fx += ("\n\nEvery fragment comes with [camera: ...]: build that picture exactly from this camera angle and "
                   "begin the prompt with it. Even inside one location the angle changes from picture to picture, so "
                   "the same room is seen from new sides: first person, over the shoulder, close-up, from above. "
                   "When the narration speaks about another place or time, the picture moves there.")
            if any(ch.get("speaker") for ch in part):
                fx += ("\n\nFragments marked [Name says] are lines spoken by that character: show that character "
                       "speaking or reacting, close enough to read the face.")
            r = c.ask("05_scenes.txt", SCHEMA_PROMPTS, 32000, extra=fx, title=title, characters=chars,
                      locations=locs, chunks=listing, orientation=orientation)
        by_index = {p.get("index"): p for p in r.get("prompts", [])}
        for i, ch in enumerate(part):
            p = by_index.get(start + i) or (r["prompts"][i] if i < len(r.get("prompts", [])) else None)
            visible = list((p or {}).get("characters") or [])
            if ch.get("speaker") in who and who[ch["speaker"]] not in visible:
                visible.append(who[ch["speaker"]])  # whoever speaks is on screen
            camera = shot_text[shots[start + i]]
            prompt = (p or {}).get("prompt") or f"A scene illustrating: {ch['text']}"
            if camera.split(",")[0].lower() not in prompt.lower():
                prompt = f"Camera: {camera}. {prompt}"
            out.append({
                "text": ch["text"],
                "paragraph_end": ch["paragraph_end"],
                "prompt": prompt,
                "shot": shots[start + i],
                "prompt_ru": (p or {}).get("prompt_ru", ""),
                "motion": media.MOTIONS[(start + i) % len(media.MOTIONS)],
                "characters": visible,
                "items": list((p or {}).get("items") or []),
                "location": (p or {}).get("location", ""),
                **({"speaker": ch["speaker"]} if ch.get("speaker") else {}),
                "effect": (p or {}).get("effect", "none"),
                "sfx": (p or {}).get("sfx", "none"),
            })
    return out


def st_scenes(c: Ctx):
    script = c.data("review")["final_script"]
    chunks = split_chunks(script, c.scene_words("short_words_per_scene" if c.short_format else "words_per_scene"),
                          c.speakers())
    c.log(f"Сценарий разбит на {len(chunks)} сцен, пишу описания картинок…")
    scenes = _prompts_for(c, chunks, c.vertical, (c.data("outline") or {}).get("title", ""))
    _name_speakers(c, scenes)
    write_json(c.out("scenes"), {"scenes": scenes})


def _name_speakers(c: Ctx, scenes: list):
    names = {ch["id"]: c.char_name(ch) for ch in c.characters}
    for sc in scenes:
        if sc.get("speaker") in names:
            sc["speaker_name"] = names[sc["speaker"]]


def st_extras(c: Ctx):
    script = c.data("review")["final_script"]
    if c.demo:
        r = _demo("extras")
    else:
        c.log("Готовлю название, описание и теги" + ("…" if c.lang == "ru" else ", перевожу сценарий на русский…"))
        r = c.ask("06_extras.txt", SCHEMA_EXTRAS, 32000, script=script)
    if c.lang == "ru" and not r.get("script_ru"):
        r["script_ru"] = script
    write_json(c.out("extras"), r)
    if c.episode:
        _record_episode(c, script)


SCHEMA_EPISODE = {"type": "object", "properties": {
    "summary": {"type": "string"}, "summary_ru": {"type": "string"}, "logline": {"type": "string"},
    "open_threads": {"type": "array", "items": {"type": "string"}}, "cliffhanger": {"type": "string"},
    "character_changes": {"type": "string"}},
    "required": ["summary", "summary_ru", "logline", "open_threads", "cliffhanger", "character_changes"]}


def _record_episode(c: Ctx, script: str):
    """Remembers what happened in this episode, so the next one continues the story."""
    from . import projects
    idea = _chosen_idea(c)
    if c.demo:
        r = {"summary": f"Demo episode {c.episode}.", "summary_ru": f"Демо-серия {c.episode}.",
             "logline": "Demo.", "open_threads": ["Where did the crew go?"], "cliffhanger": "A light on the horizon.",
             "character_changes": ""}
    else:
        c.log("Запоминаю, что произошло в этой серии, для следующих серий…")
        try:
            r = llm.call_json(c.settings, "You are the script supervisor of an animated series. Answer only with JSON.",
                              f"Episode {c.episode} script:\n\n{script}\n\nReturn:\n"
                              "- summary: 80-140 words in English: what happened, in order, with the names; the key "
                              "facts later episodes must stay consistent with;\n- summary_ru: the same in Russian;\n"
                              "- logline: one English sentence;\n- open_threads: unresolved questions and promises, "
                              "in English;\n- cliffhanger: how the episode ended, in English;\n"
                              "- character_changes: how the main characters changed or what they learned, English.",
                              SCHEMA_EPISODE, c.usage, 4000)
            c.save_usage()
        except UserError as e:
            c.log(f"Не получилось запомнить серию ({e}). Следующая серия не будет знать её сюжет.")
            return
    ch = r.pop("character_changes", "")
    if ch:
        r["summary"] = f"{r['summary']} Character changes: {ch}"
    projects.save_episode(c.project["id"], {
        "num": c.episode, "job_id": c.id, "title": idea.get("title", ""),
        "title_ru": idea.get("title_ru", "") or c.opts.get("title_ru", ""), **r})


def st_short_script(c: Ctx):
    script = c.data("review")["final_script"]
    if c.demo:
        r = _demo("short")
    else:
        c.log("Пишу сценарий шортса…")
        r = c.ask("07_short.txt", SCHEMA_SHORT, 16000, script=script)
    if c.lang == "ru" and not r.get("script_ru"):
        r["script_ru"] = r["script"]
    r["checks"] = script_checks(r["script"], int(125 * c.wpm / WORDS_PER_MIN), c.wpm)
    chunks = split_chunks(r["script"], c.scene_words("short_words_per_scene"), c.speakers())
    r["scenes"] = _prompts_for(c, chunks, True, r.get("title", ""))
    _name_speakers(c, r["scenes"])
    write_json(c.out("short_script"), r)


def st_approval(c: Ctx):
    if c.opts.get("review_pause") and not db.get(c.id).get("approved"):
        raise StopForReview()
    write_json(c.out("approval"), {"approved_at": time.time()})


# -------- voice

def _voice_list(c: Ctx, scenes: list, prefix: str):
    audio = c.dir / "audio"
    audio.mkdir(exist_ok=True)
    todo = []
    for i, sc in enumerate(scenes):
        wav = audio / f"{prefix}_{i:03d}.wav"
        if not wav.exists():
            todo.append(i)
    done = [len(scenes) - len(todo)]

    def work(i):
        c.check()
        sc = scenes[i]
        mp3 = audio / f"{prefix}_{i:03d}.mp3"
        wav = audio / f"{prefix}_{i:03d}.wav"
        if c.demo:
            media.demo_audio(mp3, max(1.5, word_count(sc["text"]) / 2.5), i)
        else:
            who = sc.get("speaker", "")
            same = lambda s: s.get("speaker", "") == who  # context only from the same voice
            prev_t = " ".join(s["text"] for s in scenes[max(0, i - 3):i] if same(s))
            next_t = scenes[i + 1]["text"] if i + 1 < len(scenes) and same(scenes[i + 1]) else ""
            settings = c.settings
            ch = next((x for x in c.characters if x["id"] == who and x.get("voice_id")), None)
            if ch:
                settings = {**c.settings, "elevenlabs_voice_id": ch["voice_id"]}
            chars = tts.synthesize(settings, sc["text"], mp3, prev_t, next_t)
            c.add_usage("tts_chars", chars)
        media.to_wav(mp3, wav)
        return i

    with ThreadPoolExecutor(max_workers=1 if c.demo else 3) as ex:
        futures = [ex.submit(work, i) for i in todo]
        for f in as_completed(futures):
            f.result()
            done[0] += 1
            yield done[0], len(scenes)
    c.save_usage()


def st_voice(c: Ctx):
    scenes = c.data("scenes")["scenes"]
    short = c.data("short_script") if c.stage_enabled("short_script") else None
    total = len(scenes) + (len(short["scenes"]) if short else 0)
    c.log(f"Озвучиваю {len(scenes)} фрагментов голосом {c.settings.get('elevenlabs_voice_name') or ''}…")
    for n, _ in _voice_list(c, scenes, "scene"):
        c.progress(n / total)
    gap, pgap = float(c.settings["scene_gap"]), float(c.settings["paragraph_gap"])
    if c.short_format:
        gap, pgap = 0.1, 0.2  # Shorts need a fast, continuous voice
    parts = [(c.dir / "audio" / f"scene_{i:03d}.wav", pgap if sc["paragraph_end"] else gap)
             for i, sc in enumerate(scenes)]
    durations = media.concat_wavs(parts, c.dir / "narration.wav")
    result = {"durations": durations, "total": sum(durations)}
    if short:
        for n, _ in _voice_list(c, short["scenes"], "short"):
            c.progress((len(scenes) + n) / total)
        sparts = [(c.dir / "audio" / f"short_{i:03d}.wav", 0.15) for i in range(len(short["scenes"]))]
        sd = media.concat_wavs(sparts, c.dir / "short_narration.wav")
        result.update(short_durations=sd, short_total=sum(sd))
    c.log(f"Озвучка готова: {result['total'] / 60:.1f} мин")
    write_json(c.out("voice"), result)


# -------- images

def _refs_for(c: Ctx, scene: dict) -> list:
    """Portraits of the recurring characters visible in this scene."""
    if not c.characters or not c.settings.get("char_reference", True) or not c.settings.get("fal_key"):
        return []
    names = {n.strip().lower() for n in scene.get("characters") or []}
    visible = [ch for ch in c.characters if {ch.get("name", "").lower(), ch.get("name_en", "").lower()} & names]
    if not visible:
        return []
    # One reference sheet per visible character: the main portrait first, then photos from other sides.
    out = []
    for ch in visible:
        paths = [c.dir / x for x in ([ch.get("portrait")] if ch.get("portrait") else []) + (ch.get("refs") or [])]
        paths = [p for p in paths if p.exists()]
        if paths:
            out.append(paths)
    return out[:images.MAX_REFERENCE_IMAGES]


def _item_refs_for(c: Ctx, scene: dict) -> list:
    """Photos of the chosen items visible in this scene: one reference sheet per item."""
    if not c.items or not c.settings.get("fal_key"):
        return []
    names = {n.strip().lower() for n in scene.get("items") or []}
    out = []
    for it in c.items:
        if {it.get("name", "").lower(), it.get("name_en", "").lower()} & names:
            paths = [c.dir / x for x in it.get("photos") or []]
            paths = [p for p in paths if p.exists()]
            if paths:
                out.append(paths)
    return out[:images.MAX_ITEM_IMAGES]


def _thumb_items(c: Ctx, prompt: str) -> list:
    named = [n for it in c.items for n in (it.get("name", ""), it.get("name_en", "")) if n and n.lower() in prompt.lower()]
    return _item_refs_for(c, {"items": named})


def _thumb_refs(c: Ctx, prompt: str) -> list:
    """The thumbnail shows the channel's own hero: the characters named in its prompt, else the host or the first one."""
    names = [n for ch in c.characters for n in (ch.get("name", ""), ch.get("name_en", "")) if n]
    named = [n for n in names if n.lower() in prompt.lower()]
    if not named and c.characters:
        main = c.host() or c.characters[0]
        named = [main.get("name") or main.get("name_en", "")]
    return _refs_for(c, {"characters": named})


def _same_place(c: Ctx) -> bool:
    """Draw one picture of every location first and use it as a reference, so all scenes there show the same place."""
    return bool(c.project.get("same_place", True)) and bool(c.settings.get("fal_key")) and not c.demo


def _location_plates(c: Ctx, scenes_by_kind: list) -> dict:
    """{(location name lowercased, vertical): plate path}. Plates are empty establishing shots of each place."""
    o = c.data("outline") or {}
    locs = {x.get("name", "").strip().lower(): x for x in o.get("locations", []) if x.get("name")}
    need = {}
    for scenes, vertical in scenes_by_kind:
        for sc in scenes:
            key = (sc.get("location") or "").strip().lower()
            if key in locs:
                need[(key, vertical)] = locs[key]
    plates = {}
    names = sorted(locs)
    for (key, vertical), loc in need.items():
        out = c.dir / "images" / f"loc_{names.index(key):02d}{'_v' if vertical else ''}.png"
        if not out.exists():
            c.check()
            c.log(f"Рисую место действия: {loc['name']}…")
            prompt = (f"Empty establishing shot of the location, no people, no characters: {loc.get('visual', '')}. "
                      "Clear, well-lit view of the whole place so it can be recognised later.")
            try:
                _make_image(c, prompt, out, vertical, 900 + names.index(key))
            except UserError as e:
                if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e):
                    raise
                c.log(f"Место «{loc['name']}» нарисовать не получилось ({e}). Сцены там будут без образца места.")
                continue
        plates[(key, vertical)] = out
    return plates


# Image models sometimes draw a vertical picture as a smaller one on white paper; this asks for the whole frame.
FULL_BLEED = ("Full-bleed image filling the entire frame edge to edge: no white borders, no margins, no frame, "
              "no letterbox bars, not a photo or poster on a background")


def _wrong_shape(path: Path) -> bool:
    from PIL import Image
    with Image.open(path) as im:
        return im.width > im.height * 0.8


def _make_image(c: Ctx, prompt: str, out: Path, vertical: bool, idx: int, fallback: Path = None, refs: list = None,
                place: Path = None, items: list = None):
    if out.exists():
        return
    if c.demo:
        media.demo_image(out, idx, vertical)
        return
    style = c.settings.get("image_style", "")
    full = f"{prompt.strip().rstrip('.')}. {style}. {FULL_BLEED}"
    if refs or place or items:
        try:
            raw = images.generate_with_reference(c.settings, full, refs or [], vertical, place=place, items=items)
            media.to_png(raw, out)
            media.trim_borders(out)
            if vertical and _wrong_shape(out):
                # The editing model sometimes keeps the shape of a reference photo: one more try, then the
                # picture is cropped to fill the frame.
                c.log(f"Картинка {idx + 1}: получилась горизонтальной, рисую ещё раз вертикально…")
                c.add_usage("image_cost", images.REFERENCE_PRICE)
                balance.record("fal", images.REFERENCE_PRICE)
                raw = images.generate_with_reference(c.settings, "Tall vertical 9:16 portrait composition. " + full,
                                                     refs or [], vertical, place=place, items=items)
                media.to_png(raw, out)
                media.trim_borders(out)
            c.add_usage("images", 1)
            c.add_usage("image_cost", images.REFERENCE_PRICE)
            balance.record("fal", images.REFERENCE_PRICE)
            return
        except UserError as e:
            if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e):
                raise
            c.log(f"Картинка {idx + 1}: по портрету не получилось ({e}). Рисую по описанию…")
    try:
        raw = images.generate(c.settings, full, vertical)
    except UserError as e:
        if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e):
            raise
        c.log(f"Картинка {idx + 1}: не получилось ({e}). Пробую смягчённое описание…")
        try:
            raw = images.generate(c.settings, "A tasteful, non-violent, non-graphic illustration. " + full, vertical)
        except UserError as e2:
            if fallback and fallback.exists():
                c.log(f"Картинка {idx + 1}: снова не получилось ({e2}). Беру предыдущую картинку.")
                shutil.copy(fallback, out)
                return
            raise
    media.to_png(raw, out)
    media.trim_borders(out)
    c.add_usage("images", 1)
    c.add_usage("image_cost", images.price_per_image(c.settings))
    balance.record(c.settings.get("image_provider") or "fal", images.price_per_image(c.settings))


def make_package(jid: str, text: str = None, side: str = None):
    """Thumbnail with headline + youtube.txt. Cheap: no API calls, can be redone any time."""
    d = job_dir(jid)
    extras = read_json(d / "extras.json") or {}
    if not extras:
        return
    opts = read_json(d / "thumb.json", {}) or {}
    if text is not None:
        opts["text"] = text
    if side is not None:
        opts["side"] = side
    write_json(d / "thumb.json", opts)
    (d / "output").mkdir(exist_ok=True)
    src = d / "images" / "thumbnail_src.png"
    job = db.get(jid) or {}
    vertical = (job.get("options") or {}).get("format") == "short"
    if src.exists():
        media.thumbnail(src, d / "output" / "thumbnail_clean.png", vertical)
        thumb.render(src, d / "output" / "thumbnail.jpg", opts.get("text", extras.get("thumbnail_text", "")),
                     opts.get("side", "left"), vertical)
        (d / "output" / "thumbnail.png").unlink(missing_ok=True)  # older versions
    short = read_json(d / "short_script.json")
    thumb.write_package(d, extras, short)


def st_images(c: Ctx):
    img = c.dir / "images"
    img.mkdir(exist_ok=True)
    scenes = c.data("scenes")["scenes"]
    jobs = [(sc["prompt"], img / f"scene_{i:03d}.png", c.vertical, i) for i, sc in enumerate(scenes)]
    refs = {img / f"scene_{i:03d}.png": _refs_for(c, sc) for i, sc in enumerate(scenes)}
    short = c.data("short_script") if c.stage_enabled("short_script") else None
    if short:
        jobs += [(sc["prompt"], img / f"short_{i:03d}.png", True, i) for i, sc in enumerate(short["scenes"])]
        refs.update({img / f"short_{i:03d}.png": _refs_for(c, sc) for i, sc in enumerate(short["scenes"])})
    item_refs = {img / f"scene_{i:03d}.png": _item_refs_for(c, sc) for i, sc in enumerate(scenes)}
    if short:
        item_refs.update({img / f"short_{i:03d}.png": _item_refs_for(c, sc) for i, sc in enumerate(short["scenes"])})
    places = {}
    if _same_place(c):
        kinds = [(scenes, c.vertical)] + ([(short["scenes"], True)] if short else [])
        plates = _location_plates(c, kinds)
        for scs, vertical, prefix in [(scenes, c.vertical, "scene")] + ([(short["scenes"], True, "short")] if short else []):
            for i, sc in enumerate(scs):
                pl = plates.get(((sc.get("location") or "").strip().lower(), vertical))
                if pl:
                    places[img / f"{prefix}_{i:03d}.png"] = pl
    extras = c.data("extras") or {}
    if extras.get("thumbnail_prompt"):
        jobs.append((extras["thumbnail_prompt"], img / "thumbnail_src.png", c.vertical, 999))
        refs[img / "thumbnail_src.png"] = _thumb_refs(c, extras["thumbnail_prompt"])
        item_refs[img / "thumbnail_src.png"] = _thumb_items(c, extras["thumbnail_prompt"])
    todo = [j for j in jobs if not j[1].exists()]
    c.log(f"Генерирую картинки: {len(todo)} из {len(jobs)} (сервис: "
          f"{images.PROVIDERS.get(c.settings['image_provider'], {}).get('name', '')})…")
    done = len(jobs) - len(todo)
    failed = []

    def work(j):
        c.check()
        prompt, out, vertical, idx = j
        try:
            _make_image(c, prompt, out, vertical, idx, refs=refs.get(out), place=places.get(out),
                        items=item_refs.get(out))
        except UserError as e:
            if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e):
                raise
            failed.append(j)
            c.log(f"Картинка {idx + 1}: ошибка — {e}")

    with ThreadPoolExecutor(max_workers=1 if c.demo else max(1, int(c.settings["image_parallel"]))) as ex:
        futures = [ex.submit(work, j) for j in todo]
        for f in as_completed(futures):
            f.result()
            done += 1
            c.progress(done / len(jobs))
            c.save_usage()
    # Images that still failed: reuse the neighbour so the video can be built.
    for prompt, out, vertical, idx in failed:
        prefix = out.name.split("_")[0]
        neighbours = [img / f"{prefix}_{k:03d}.png" for k in (idx - 1, idx + 1, idx - 2, idx + 2)]
        src = next((n for n in neighbours if n.exists()), None)
        if out.name == "thumbnail_src.png":
            continue
        if not src:
            raise UserError(f"Не удалось сделать картинку {idx + 1}. Попробуйте шаг ещё раз или смените сервис.")
        shutil.copy(src, out)
        c.log(f"Картинка {idx + 1}: подставлена соседняя. Можно перегенерировать её вручную.")
    make_package(c.id)
    c.save_usage()
    write_json(c.out("images"), {"count": len(jobs), "replaced": len(failed)})


# -------- video

def subs_on(job_or_opts, kind: str, settings: dict = None) -> bool:
    """Whether subtitles are burned into the long video ("video") or the short ("short")."""
    opts = job_or_opts.get("options", job_or_opts)
    key = "subs_video" if kind == "video" else "subs_short"
    if key in opts:
        return bool(opts[key])
    settings = settings or load_settings()
    return bool(settings.get("subtitles_long" if kind == "video" else "subtitles_short"))


# -------- animation

def job_language(job: dict) -> str:
    opts = job.get("options", {})
    if opts.get("language"):
        return opts["language"]
    from . import projects
    return projects.get(opts.get("project_id", "")).get("language") or "en"


def fx_mode(job_or_opts) -> str:
    """Effects mode of a job. Jobs made before this feature have none."""
    opts = job_or_opts.get("options", job_or_opts)
    return opts.get("fx_mode") if opts.get("fx_mode") in effects.MODES else "off"


def anim_options(job_or_opts) -> tuple:
    """(mode, animate the short too). Jobs made before this feature have no animation."""
    opts = job_or_opts.get("options", job_or_opts)
    mode = opts.get("anim_mode") or "off"
    return (mode if mode in animate.MODES else "off"), bool(opts.get("anim_short"))


def anim_count(job_or_opts) -> int:
    opts = job_or_opts.get("options", job_or_opts)
    return animate.clamp_count(opts.get("anim_count", animate.COUNT_DEFAULT))


def anim_scenes(job_or_opts) -> list:
    opts = job_or_opts.get("options", job_or_opts)
    return [int(i) for i in (opts.get("anim_scenes") or []) if str(i).lstrip("-").isdigit()]


def needs_anim_choice(job: dict) -> bool:
    """The owner asked to stop before animation and has not chosen the scenes yet."""
    opts = job.get("options", {})
    # Only after the script is approved: earlier review stops (topics, script) are not about animation.
    return (job.get("status") == "review" and bool(opts.get("anim_pause")) and not opts.get("anim_picked")
            and bool(job.get("id")) and (job_dir(job["id"]) / "approval.json").exists())


def anim_suggest(jid: str, job: dict) -> list:
    """Scenes the program would animate by itself: shown checked during the pause, the owner adds or removes."""
    if not needs_anim_choice(job):
        return []
    scenes = (read_json(job_dir(jid) / "scenes.json") or {}).get("scenes") or []
    mode = anim_options(job)[0]
    return animate.pick(mode, scenes, anim_count(job), anim_scenes(job)) if scenes else []


def anim_short_pick(short_scenes: list, mode: str, count: int) -> list:
    """Short: the first scenes, never more than `count` (all of them only in the "all" mode)."""
    if mode == "off":
        return []
    n = len(short_scenes)
    return list(range(n if mode == "all" else min(n, count)))


def anim_quote(jid: str, mode: str, anim_short: bool, count: int = animate.COUNT_DEFAULT, custom=None) -> dict:
    """How many new clips a change of animation would order and what they cost. Clips made earlier are free."""
    d = job_dir(jid)
    settings = load_settings()
    try:
        scenes = read_json(d / "scenes.json")["scenes"]
        voice = read_json(d / "voice.json")
    except (OSError, ValueError, KeyError, TypeError):
        return {"new": 0, "kept": 0, "cost": 0.0, "known": False}
    sel = animate.pick(mode, scenes, count, custom)
    items = [("scene", i, voice["durations"][i]) for i in sel]
    short = read_json(d / "short_script.json") if (d / "short_script.json").exists() else None
    if short and anim_short:
        sd = voice.get("short_durations", [])
        items += [("short", i, sd[i] if i < len(sd) else 5.0)
                  for i in anim_short_pick(short["scenes"], mode, count)]
    new = [w for w in items if not (d / "anim" / f"{w[0]}_{w[1]:03d}.mp4").exists()]
    return {"new": len(new), "kept": len(items) - len(new), "known": True,
            "cost": round(sum(animate.cost(settings, w[2]) for w in new), 2)}


def st_animate(c: Ctx):
    job = db.get(c.id) or {"options": c.opts}
    if job["options"].get("anim_pause") and not job["options"].get("anim_picked"):
        raise StopForReview("⏸ Картинки и озвучка готовы. Отметьте в шаге «Анимация» сцены, которые нужно оживить, "
                            "и нажмите «Оживить выбранные и продолжить».")
    mode, anim_short = anim_options(job)
    count = anim_count(job)
    scenes = c.data("scenes")["scenes"]
    voice = c.data("voice")
    sel = animate.pick(mode, scenes, count, anim_scenes(job))
    short = c.data("short_script") if c.stage_enabled("short_script") else None
    ssel = anim_short_pick(short["scenes"], mode, count) if short and anim_short else []
    result = {"mode": mode, "model": c.settings.get("video_model"), "scene": sel, "short": ssel, "failed": []}
    if not sel and not ssel:
        c.log("Анимация выключена: в видео будут картинки с плавным движением камеры.")
        write_json(c.out("animate"), result)
        return
    adir = c.dir / "anim"
    adir.mkdir(exist_ok=True)
    work_items = [("scene", i, scenes[i], voice["durations"][i], c.vertical) for i in sel]
    work_items += [("short", i, short["scenes"][i], voice["short_durations"][i], True) for i in ssel]
    todo = [w for w in work_items if not (adir / f"{w[0]}_{w[1]:03d}.mp4").exists()]
    est = sum(animate.cost(c.settings, w[3]) for w in todo) if not c.demo else 0
    c.log(f"Оживляю сцены: {len(todo)} из {len(work_items)} "
          f"({animate.model(c.settings)['name'].split(' — ')[0]}, примерно ${est:.2f})…")
    done = [len(work_items) - len(todo)]

    def work(w):
        prefix, i, sc, need, vertical = w
        c.check()
        out = adir / f"{prefix}_{i:03d}.mp4"
        img = c.dir / "images" / f"{prefix}_{i:03d}.png"
        try:
            if c.demo:
                w_, h_ = (720, 1280) if vertical else (1280, 720)
                media.render_clip(img, out, int(5 * 25), w_, h_, 25, "zoom_in", 0.3)
            else:
                data = animate.generate(c.settings, img, sc["prompt"], need, vertical, c.check)
                tmp = out.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(out)
                c.add_usage("anim_cost", animate.cost(c.settings, need))
                balance.record("fal", animate.cost(c.settings, need))
        except UserError as e:
            if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e):
                raise
            result["failed"].append(f"{prefix}_{i:03d}")
            c.log(f"Анимация сцены {i + 1}{' (шортс)' if vertical else ''} не получилась: {e}. Будет обычная картинка.")

    with ThreadPoolExecutor(max_workers=1 if c.demo else 3) as ex:
        futures = [ex.submit(work, w) for w in todo]
        for f in as_completed(futures):
            f.result()
            done[0] += 1
            c.progress(done[0] / len(work_items))
            c.save_usage()
    c.save_usage()
    write_json(c.out("animate"), result)


def _render(c: Ctx, prefix: str, scenes: list, durations: list, narration: Path, out: Path,
            clips_dir: Path, width: int, height: int, subs: bool = False):
    fps = int(c.settings["fps"])
    clips_dir.mkdir(exist_ok=True)
    bounds, t = [], 0.0
    for d in durations:
        bounds.append((round(t * fps), round((t + d) * fps)))
        t += d
    total = t
    clips = [clips_dir / f"{prefix}_{i:03d}.mp4" for i in range(len(scenes))]
    todo = [i for i in range(len(scenes)) if not clips[i].exists()]
    done = [len(scenes) - len(todo)]
    strength = float(c.settings["motion_strength"])
    anim = set((read_json(c.out("animate"), {}) or {}).get(prefix, []))
    fx = effects.plan(scenes, fx_mode(db.get(c.id) or c.opts), height > width)

    def work(i):
        c.check()
        a, b = bounds[i]
        src = c.dir / "anim" / f"{prefix}_{i:03d}.mp4"
        if i in anim and src.exists():
            media.render_anim_clip(src, clips[i], b - a, width, height, fps,
                                   fade_in=(i == 0), fade_out=(i == len(scenes) - 1), effect=fx[i][1])
            return
        media.render_clip(c.dir / "images" / f"{prefix}_{i:03d}.png", clips[i], b - a, width, height, fps,
                          scenes[i]["motion"], strength, fade_in=(i == 0), fade_out=(i == len(scenes) - 1),
                          effect=fx[i][1])

    with ThreadPoolExecutor(max_workers=max(1, int(c.settings["render_parallel"]))) as ex:
        futures = [ex.submit(work, i) for i in todo]
        for f in as_completed(futures):
            f.result()
            done[0] += 1
            c.progress(0.9 * done[0] / len(scenes))
    c.check()
    c.log("Склеиваю фрагменты и накладываю звук…")
    joined = clips_dir / "joined.mp4"
    media.concat_clips(clips, joined)
    music = _music_for(c, total)
    out.parent.mkdir(exist_ok=True)
    cap = captions.build(c.dir, prefix, scenes, durations, width, height)
    sfx_file = clips_dir / "sfx.wav"
    has_sfx = effects.build_track(fx, durations, sfx_file, float(c.settings.get("sfx_volume", 0.5)))
    if has_sfx:
        c.log(f"Звуковые эффекты: {sum(1 for f in fx if f[2] != 'none')}")
    if subs:
        c.log("Накладываю субтитры на видео…")
    media.final_mix(joined, narration, music, out, float(c.settings["music_volume"]),
                    float(c.settings["loudness_lufs"]), total,
                    subs=cap["ass"] if subs else None, fonts=cap["fonts"], sfx=sfx_file if has_sfx else None)
    joined.unlink(missing_ok=True)
    sfx_file.unlink(missing_ok=True)
    return {"file": str(out.relative_to(c.dir)), "duration": total, "music": music.name if music else "",
            "subtitles": subs}


def _music_for(c: Ctx, total: float):
    """Background track: none, a fitting one from the music folder, or a new one composed for this video."""
    mode = c.opts.get("music_mode") or "library"
    if mode == "off":
        c.log("Музыка выключена для этого ролика.")
        return None
    extras = c.data("extras") or {}
    mood = " ".join([extras.get("music_style_ru", ""), extras.get("music_prompt", "")])
    own = c.dir / "music.mp3"
    if mode == "generate" and not c.demo:
        if own.exists():
            return own
        prompt = extras.get("music_prompt") or f"Instrumental background music for a video about: {c.niche}"
        style = extras.get("music_style_ru") or "фоновая музыка"
        sec = musiclib.length_for(total, c.vertical)
        c.log(f"Сочиняю музыку «{style}» ({sec:.0f} сек, ElevenLabs, примерно ${musiclib.cost(sec):.2f})…")
        try:
            title = extras.get("youtube_title_ru") or extras.get("youtube_title") or c.job.get("title") or ""
            track = musiclib.compose(c.settings, prompt, sec, style, title)
            shutil.copy(track, own)
            c.add_usage("music_cost", musiclib.cost(sec))
            c.save_usage()
            c.log(f"Музыка готова и сохранена в раздел «Музыка»: {track.name}")
            return own
        except UserError as e:
            c.log(f"Не получилось сочинить музыку ({e}). Беру трек из раздела «Музыка».")
    track = musiclib.pick(mood)
    if track:
        c.log(f"Фоновая музыка: {track.name}")
    else:
        c.log("Фоновой музыки нет (раздел «Музыка» пуст), ролик будет без музыки.")
    return track


def st_video(c: Ctx):
    scenes = c.data("scenes")["scenes"]
    voice = c.data("voice")
    w, h = (1080, 1920) if c.vertical else (1920, 1080)
    c.log(f"Собираю {'шортс' if c.vertical else 'видео'} {w}×{h} из {len(scenes)} сцен…")
    r = _render(c, "scene", scenes, voice["durations"], c.dir / "narration.wav",
                c.dir / "output" / "video.mp4", c.dir / "clips", w, h, subs_on(db.get(c.id) or c.opts, "video", c.settings))
    r["vertical"] = c.vertical
    c.log(f"Видео готово: {r['duration'] / 60:.1f} мин" if not c.vertical else f"Шортс готов: {r['duration']:.0f} сек")
    write_json(c.out("video"), r)
    _respeed_after_build(c, "video")


def st_short_video(c: Ctx):
    short = c.data("short_script")
    voice = c.data("voice")
    c.log("Собираю вертикальный шортс 1080×1920…")
    r = _render(c, "short", short["scenes"], voice["short_durations"], c.dir / "short_narration.wav",
                c.dir / "output" / "short.mp4", c.dir / "clips_short", 1080, 1920, subs_on(db.get(c.id) or c.opts, "short", c.settings))
    write_json(c.out("short_video"), r)
    _respeed_after_build(c, "short")


# -------- speed of a finished video

SPEEDS = (1.0, 1.05, 1.1, 1.15, 1.2, 1.25, 1.3, 1.4, 1.5)


def _speed_files(d: Path, kind: str, vertical: bool):
    """(video, original copy, subtitles, original subtitles) of the long video or the Short."""
    name = "video" if kind == "video" else "short"
    srt = "short_subtitles" if (kind == "short" or vertical) else "subtitles"
    out = d / "output"
    return out / f"{name}.mp4", out / f"{name}_orig.mp4", out / f"{srt}.srt", out / f"{srt}_orig.srt"


def _scale_srt(src: Path, dst: Path, speed: float):
    def fix(m):
        h, mi, s, ms = (int(x) for x in m.groups())
        total = round((((h * 60 + mi) * 60 + s) * 1000 + ms) / speed)
        return f"{total // 3600000:02d}:{total // 60000 % 60:02d}:{total // 1000 % 60:02d},{total % 1000:03d}"
    dst.write_text(re.sub(r"(\d+):(\d+):(\d+),(\d+)", fix, src.read_text("utf-8")), "utf-8")


def set_speed(jid: str, kind: str, speed: float) -> dict:
    """Speeds up (or restores) a finished video: picture and voice together, the voice keeps its pitch.
    Always made from the original assembly, so changing the speed again does not lose quality."""
    speed = min(SPEEDS, key=lambda x: abs(x - float(speed)))
    j = db.get(jid) or {}
    d = job_dir(jid)
    vertical = (j.get("options") or {}).get("format") == "short"
    mp4, orig, srt, srt_orig = _speed_files(d, kind, vertical)
    stage = d / ("video.json" if kind == "video" else "short_video.json")
    info = read_json(stage, {}) or {}
    if not mp4.exists() and not orig.exists():
        raise UserError("Видео ещё не готово.")
    if speed == 1.0:
        if orig.exists():
            orig.replace(mp4)
        if srt_orig.exists():
            srt_orig.replace(srt)
    else:
        if not orig.exists():
            shutil.copy(mp4, orig)
            if srt.exists():
                shutil.copy(srt, srt_orig)
        tmp = mp4.with_suffix(".speed.mp4")
        media.run(["-i", orig, "-filter_complex", f"[0:v]setpts=PTS/{speed}[v];[0:a]atempo={speed}[a]",
                   "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", 20,
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", 44100, "-movflags", "+faststart",
                   tmp], "Ускорение видео")
        tmp.replace(mp4)
        if srt_orig.exists():
            _scale_srt(srt_orig, srt, speed)
    base = info.get("orig_duration") or info.get("duration") or 0
    if info:
        info.update(speed=speed, orig_duration=base, duration=round(base / speed, 2) if base else info.get("duration"))
        write_json(stage, info)
    opts = dict(j.get("options") or {})
    opts["speed_video" if kind == "video" else "speed_short"] = speed
    db.update(jid, options=opts)
    return {"speed": speed, "duration": info.get("duration")}


def _respeed_after_build(c: Ctx, kind: str):
    """A fresh assembly replaces the original; the chosen speed is applied to it again."""
    _, orig, _, srt_orig = _speed_files(c.dir, kind, c.vertical)
    orig.unlink(missing_ok=True)
    srt_orig.unlink(missing_ok=True)
    speed = float(((db.get(c.id) or {}).get("options") or {}).get("speed_video" if kind == "video" else "speed_short") or 1)
    if speed != 1.0:
        c.log(f"Ускоряю {'видео' if kind == 'video' else 'шортс'} в {speed:g} раза…")
        set_speed(c.id, kind, speed)


STAGE_FUNCS = {
    "idea": st_idea, "outline": st_outline, "script": st_script, "review": st_review,
    "scenes": st_scenes, "extras": st_extras, "short_script": st_short_script, "approval": st_approval,
    "voice": st_voice, "images": st_images, "animate": st_animate, "video": st_video, "short_video": st_short_video,
}


def run_job(job: dict):
    """Runs all unfinished stages. Returns the final status."""
    c = Ctx(job)
    for sid in STAGE_IDS:
        if not c.stage_enabled(sid) or c.out(sid).exists():
            continue
        c.check()
        c.stage = sid
        c.progress(0)
        STAGE_FUNCS[sid](c)
        c.log(f"✓ {STAGE_NAMES[sid]}")
    c.stage = "done"
    db.update(c.id, progress=100, stage="done")
    cost = c.usage.get("llm_cost", 0) + c.usage.get("image_cost", 0) + c.usage.get("anim_cost", 0)
    c.log(f"Готово. Примерная стоимость: ${cost:.2f} + {int(c.usage.get('tts_chars', 0))} символов ElevenLabs")


# What has to be thrown away when a stage is redone (paths relative to the job folder).
# Text stages up to "scenes" change everything after them; later stages only touch what depends on them.
RESET_PLAN = {
    "extras": ["extras.json", "images/thumbnail_src.png", "images.json", "output/youtube.txt",
               "output/thumbnail.jpg", "output/thumbnail_clean.png", "output/thumbnail.png", "thumb.json"],
    "short_script": ["short_script.json", "anim/short_*", "animate.json", "audio/short_*", "short_narration.wav", "voice.json", "images/short_*",
                     "images.json", "clips_short", "short_video.json", "output/short.mp4"],
    "approval": ["approval.json"],
    "voice": ["voice.json", "audio", "narration.wav", "short_narration.wav", "animate.json", "video.json", "clips",
              "output/video.mp4", "short_video.json", "clips_short", "output/short.mp4"],
    "images": ["images.json", "images", "anim", "animate.json", "output/thumbnail.jpg", "output/thumbnail_clean.png", "video.json",
               "clips", "output/video.mp4", "short_video.json", "clips_short", "output/short.mp4"],
    "animate": ["animate.json", "anim", "video.json", "clips", "output/video.mp4", "short_video.json", "clips_short",
                "output/short.mp4"],
    "video": ["video.json", "clips", "output/video.mp4", "subtitles.ass", "output/subtitles.srt", "output/video_orig.mp4"],
    "short_video": ["short_video.json", "clips_short", "output/short.mp4", "short_subtitles.ass", "output/short_subtitles.srt", "output/short_orig.mp4"],
}


def _remove(d: Path, pattern: str):
    for p in d.glob(pattern):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def reset_from(jid: str, stage: str):
    """Delete the results of `stage` and whatever depends on it."""
    d = job_dir(jid)
    idx = STAGE_IDS.index(stage)
    if stage in RESET_PLAN:
        for pattern in RESET_PLAN[stage]:
            _remove(d, pattern)
    else:
        for sid in STAGE_IDS[idx:]:
            (d / f"{sid}.json").unlink(missing_ok=True)
        (d / "review_progress.json").unlink(missing_ok=True)
        # New text means new voice, images and clips.
        for rel in ("audio", "images", "anim", "clips", "clips_short", "output", "narration.wav", "short_narration.wav",
                    "thumb.json"):
            _remove(d, rel)
    if stage not in RESET_PLAN or stage == "approval":
        db.update(jid, approved=0)


def regenerate_image(jid: str, kind: str, index: int, prompt: str = None):
    """Delete one image (and its clip) so the next run re-creates it with an optional new prompt."""
    d = job_dir(jid)
    if kind == "thumbnail":
        ex = read_json(d / "extras.json")
        if prompt and ex:
            ex["thumbnail_prompt"] = prompt
            write_json(d / "extras.json", ex)
        (d / "images" / "thumbnail_src.png").unlink(missing_ok=True)
        (d / "images.json").unlink(missing_ok=True)
        return
    if kind == "short":
        data = read_json(d / "short_script.json")
        scenes = data["scenes"]
    else:
        data = read_json(d / "scenes.json")
        scenes = data["scenes"]
    if prompt:
        scenes[index]["prompt"] = prompt
        write_json(d / ("short_script.json" if kind == "short" else "scenes.json"), data)
    prefix = "short" if kind == "short" else "scene"
    (d / "images" / f"{prefix}_{index:03d}.png").unlink(missing_ok=True)
    (d / "anim" / f"{prefix}_{index:03d}.mp4").unlink(missing_ok=True)
    (d / "animate.json").unlink(missing_ok=True)
    (d / ("clips_short" if kind == "short" else "clips") / f"{prefix}_{index:03d}.mp4").unlink(missing_ok=True)
    (d / "images.json").unlink(missing_ok=True)
    (d / ("short_video.json" if kind == "short" else "video.json")).unlink(missing_ok=True)
    (d / "output" / ("short.mp4" if kind == "short" else "video.mp4")).unlink(missing_ok=True)


# ---------------------------------------------------------------- demo data

DEMO_SCRIPT = (
    "In the winter of 1872, a ship was found drifting in the Atlantic. Her sails were set. Her cargo was untouched. "
    "But not a single soul was on board.\n\n"
    "The captain's table was still laid for breakfast. A child's toys lay on the floor of his cabin. "
    "The lifeboat was gone.\n\n"
    "What happened to the ten people on the Mary Celeste is still one of the sea's great mysteries. "
    "Subscribe for more stories like this."
)


def _demo(kind: str, script: str = ""):
    if kind == "idea":
        return {"ideas": [{"title": "The Ghost Ship Mary Celeste", "title_ru": "Корабль-призрак «Мария Селеста»",
                           "premise": "A ship found with no crew.", "premise_ru": "Корабль нашли без экипажа.",
                           "hook_angle": "Where did everyone go?",
                           "scores": {"curiosity": 9, "emotion": 8, "clarity": 9, "uniqueness": 6, "visual": 9}}],
                "chosen_index": 0, "reason_ru": "Демо-режим: проверка установки без ключей."}
    if kind == "outline":
        return {"title": "The Ghost Ship Mary Celeste",
                "hook": {"text_idea": "Ship found empty", "big_question": "Where did the crew go?",
                         "why_ru": "Демо"},
                "sections": [{"name": "Hook", "start_time": "0:00", "purpose_ru": "Демо", "beats": ["empty ship"],
                              "rehook": "", "approx_words": 80}],
                "open_loops": [], "characters": [], "locations": [], "ending_payoff": "Mystery",
                "summary_ru": "Демо-ролик для проверки установки."}
    if kind == "script":
        return {"script": DEMO_SCRIPT}
    if kind == "review":
        return {"scores": [{"criterion": k, "score": 9, "comment_ru": "Демо"} for k in CRITERIA_RU],
                "issues_ru": [], "verdict_ru": "Демо-режим.", "revised_script": script}
    if kind == "extras":
        return {"script_ru": "Зимой 1872 года в Атлантике нашли дрейфующий корабль…",
                "youtube_title": "The Ghost Ship Mary Celeste", "youtube_title_ru": "Корабль-призрак",
                "description": "Demo", "tags": ["demo"], "thumbnail_text": "GHOST SHIP",
                "thumbnail_text_ru": "КОРАБЛЬ-ПРИЗРАК", "thumbnail_prompt": "demo"}
    if kind == "short":
        return {"title": "Ghost ship", "title_ru": "Корабль-призрак",
                "script": "A ship was found with her sails set and no one on board. Breakfast was still on the table. "
                          "The full story is on our channel.",
                "script_ru": "Корабль нашли с поднятыми парусами и без людей.", "why_ru": "Демо",
                "hook_score": 9, "payoff_score": 8}
    return {}
