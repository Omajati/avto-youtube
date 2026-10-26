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

from . import animate, balance, captions, cta, db, effects, graphics, images, llm, media, music as musiclib, take, thumb, tts
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
    "hook": "Хук (первые секунды)",
    "open_loops": "Открытые вопросы (интрига)",
    "rehooks": "Удерживающие крючки",
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
TEMPO_MIN, TEMPO_MAX = 0.8, 1.6
WPM = {"en": 150, "ru": 125, "de": 125, "es": 160, "fr": 150}  # longer words (Russian, German) fit fewer in a minute

LANG_OVERRIDE = {
    "ru": """

LANGUAGE OVERRIDE (this rule beats every instruction above and below about language and audience):
- This channel is Russian-language. The audience is Russian-speaking viewers in Russia and the CIS, not US/UK viewers.
- Write every spoken or published text in natural, lively spoken Russian: ideas and titles, hooks, the script, the Short, YouTube titles, descriptions, tags, hashtags and thumbnail text. Never write them in English.
- The spoken text (script and Short) is read by a voice engine: in it always write numbers, dates, money, percentages and abbreviations in words, in the right grammatical case, the way a Russian narrator says them ("в двух тысячах рублей", not "в 2000 ₽"; "то есть", not "т.е."). There write foreign names in Cyrillic as Russians pronounce them. Titles, descriptions and thumbnail text may use digits.
- Image prompts (fields named prompt and thumbnail_prompt) stay in English, because the image model understands English best.
- Fields ending in _ru simply repeat the same Russian text. Exception: script_ru must be an empty string (the script is already in Russian).
- The Short is 90-115 Russian words; its last line sends viewers to the full story on the channel in Russian.""",
}

# German, Spanish and French channels: everything the viewer hears or reads is in that language, while the owner
# (who reads Russian) gets every _ru field and script_ru as a Russian translation.
FOREIGN = {
    "de": ("German", "German-speaking viewers in Germany, Austria and Switzerland", "Germans", 100, 125,
           '"zweitausend Euro", not "2.000 €"; "zum Beispiel", not "z. B."'),
    "es": ("Spanish", "Spanish-speaking viewers in Spain and Latin America", "Spanish speakers", 120, 145,
           '"dos mil euros", not "2.000 €"; "por ejemplo", not "p. ej."'),
    "fr": ("French", "French-speaking viewers in France, Belgium, Switzerland and Canada", "French speakers", 110, 135,
           '"deux mille euros", not "2 000 €"; "par exemple", not "p. ex."'),
}
for _code, (_name, _aud, _people, _lo, _hi, _ex) in FOREIGN.items():
    LANG_OVERRIDE[_code] = f"""

LANGUAGE OVERRIDE (this rule beats every instruction above and below about language and audience):
- This channel is {_name}-language. The audience is {_aud}, not US/UK viewers.
- Write every spoken or published text in natural, lively spoken {_name}: ideas and titles, hooks, the script, the Short, YouTube titles, descriptions, tags, hashtags and thumbnail text. Never write them in English or Russian. Write as a native {_name} author would, with idioms and humour that land for {_people}, not as a translation.
- The spoken text (script and Short) is read by a voice engine: in it always write numbers, dates, money, percentages and abbreviations in words, the way a native narrator says them ({_ex}). Titles, descriptions and thumbnail text may use digits.
- Image prompts (fields named prompt and thumbnail_prompt) stay in English, because the image model understands English best.
- The channel owner reads only Russian: every field ending in _ru is a Russian translation of its {_name} counterpart, and script_ru is the full script translated into Russian.
- The Short is {_lo}-{_hi} {_name} words; its last line sends viewers to the full story on the channel in {_name}."""


class StopForReview(Exception):
    pass


PACE_FILE = JOBS_DIR.parent / "voice_pace.json"
_pace_lock = threading.Lock()


def measured_wpm(key: str):
    """How fast this voice really speaks (words per minute, pauses included), from its last videos, or None.
    The script length is set by it, so a 10-minute video comes out about 10 minutes long."""
    try:
        got = (json.loads(PACE_FILE.read_text("utf-8")) or {}).get(key) or []
    except (OSError, ValueError):
        return None
    return round(statistics.median(got)) if got else None


def record_wpm(key: str, wpm: float):
    if not 80 <= wpm <= 260:
        return
    with _pace_lock:
        try:
            data = json.loads(PACE_FILE.read_text("utf-8")) if PACE_FILE.exists() else {}
        except (OSError, ValueError):
            data = {}
        data[key] = (data.get(key) or [])[-7:] + [round(wpm, 1)]
        PACE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


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
    return len(re.findall(r"[^\W_]+(?:['’-][^\W_]+)*", text))


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
        sentences = re.split(r"(?:(?<=[.!?…])|(?<=[.!?…][\"”’')\]»])|(?<=[.!?…][\"”’')\]»]{2}))\s+(?=[A-ZÀ-ÖØ-ÞА-ЯЁ0-9\"“„‘'(«—¿¡])", para)
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
- This video is a vertical YouTube Short of about {seconds} seconds (about {words} spoken words), not a long video. Ignore the long-video rules: the 35-45-word hook, setup timings in minutes, 2-3 twists, open loops, re-hooks every 60-90 seconds, sensory scene-setting (weather, smells, "imagine standing there") and dates or places added only for atmosphere.

HOW A SHORT IS WATCHED (build everything around this):
- The viewer decides in the first 1-2 seconds whether to swipe. They stay only while they cannot predict what happens next. The moment the ending becomes obvious, they leave.
- The deadliest mistake is the predictable story: "a problem appears, the hero solves it the obvious way, everyone is happy". If the viewer can guess the ending from the first sentence, the idea is rejected, however neat it is.
- If the hero is a product, mascot or brand character, the hero simply winning is an advert, and nobody watches adverts. The hero must be underestimated, mocked, fail first, face a rival or a worse problem, or win in an absurd, unexpected way that costs something. Comedy and character beat demonstration.

SHORT STORY ENGINE (every idea, outline and script must have all four):
1) HOOK, the first sentence (2 seconds): drop the viewer into the most extreme moment, already in motion. Strong hook types: an absurd or impossible situation ("I tried to fix the leak and flooded three floors of neighbours."); a conflict mid-fight; a bold claim that sounds wrong; a ticking clock with high or ridiculous stakes; a forbidden thing or a secret; a "this went horribly wrong" opening; a direct challenge to the viewer. Never start with background, a name introduction, the weather or "one day".
2) ESCALATION (to about 40%): things get worse in a way the viewer did not expect. The obvious solution fails or is blocked. Raise the stakes with a concrete complication.
3) TWIST (about 70-80%): a turn nobody saw coming that changes the meaning of what happened: a reveal, a role reversal, an ironic consequence, an absurd solution, the "villain" was right, the hero's win backfires. The twist must be set up by an earlier detail so it feels clever, not random.
4) PUNCHLINE (the last 1-2 sentences): a sharp, funny or bitter final line that rewards the viewer. Ideally it loops into the first sentence so the replay feels seamless. No moral lecture, no summary, no "and so we learned".

PROVEN SHORT FORMATS (pick the one that fits the idea, or mix):
- The insider and the believer: a naive character trusts what everyone is told, a cunning, confident character knows how it really works and reveals it ("what nobody tells you about..."). The hook names a familiar thing and promises the hidden truth; the twist is the reveal; the punchline is the believer's reaction or the insider's cynical last word. If the channel has two recurring characters, the same duo in every video builds a fan base; with one host, he is the insider and the viewer is the believer.
- The bet or duel (needs two characters): two characters bet on who is right; the obvious favourite loses or wins in a way nobody expected.
- The disaster: something goes spectacularly wrong from the first second; the story is how it got worse and how it ended absurdly.
- The secret: the hero hides something or discovers something hidden; the last line reveals it.

RETENTION RULES:
- Every sentence either answers the previous question while opening a new one, or raises the stakes. Cut any sentence that only explains.
- Show conflict between characters: they want different things, argue, bet, sabotage. When characters speak with their own voices, dialogue lines with personality beat narration.
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
- No subscribe line and no sign-off at all: the Short ends on the punchline, the last word is the punchline. An animated subscribe button is added to the video automatically. Closing lines like "ладно, поехали дальше", "подписывайся", "вот такие дела" tell the viewer the video is over, and they swipe away before the end, which kills retention.
- The first words are the hook itself. A framing opening ("Вёз я вчера мужика...", "Сегодня расскажу...", "Знаете, что...") wastes the two seconds that decide everything: first the sharpest claim, accusation or absurd fact aimed at the viewer, then the story that proves it.
- The twist must be logical and earned: the viewer should think "oh, of course", not "that is random". A silly coincidence or an unrelated surprise is not a twist.
- Scenes change every 3-5 seconds, so image prompts must be bold and readable on a phone: one clear subject, close framing, strong contrast.
- Title: max 60 characters, curiosity-driven, never reveals the twist. Tags and description are for Shorts.

SWIPE TEST (the editor applies it to every Short): read the script line by line as a bored viewer with a thumb over the screen. Mark the first line where you would swipe away (too slow, predictable, explaining, a warm-up, a sign-off) and rewrite or cut it; repeat until no such line is left. Average viewed percentage is the main number YouTube judges a Short by."""


SCRIPT_CRAFT = """

SCRIPTWRITER TRAINING (always on; the owner's niche and character rules still win where they differ):
The only job of a script is to be watched to the end. A line that is merely correct, useful or neat but not interesting is a failed line. Before writing, ask: why would a stranger with a thumb over the screen keep watching THIS sentence?
1) ONE THOUGHT per video, retellable in one plain sentence. Two thoughts are two videos. Everything that does not serve this thought is cut, however good it is.
2) THE HOOK IS WRITTEN LAST: first build the story, then pull its strongest moment, claim or number to the very first words. No greeting, no self-introduction, no "today I will tell", no announcing what comes; start doing it. Hook types (rotate them between videos, never the same type twice in a row): a shocking number; a claim that sounds wrong; a costly mistake; a scene already in motion; a promise with a condition; a question with a gap the viewer must close; a direct callout of the viewer ("если ты ... , ты теряешь ...").
3) KEEP THE HOOK'S PROMISE: the video delivers exactly what the hook and the title promise, no bait. A broken promise makes viewers leave and never come back.
4) OPEN LOOPS: every block answers the previous question and at the same moment opens a new one ("но это было не самое странное", only if something stranger really follows). Close one loop before opening the next; never leave the main question open at the end.
5) VALUE EARLY AND SPREAD OUT: the first real payoff comes right after the hook, and something new (a fact, number, turn, example, joke, reveal) arrives regularly: every few seconds in a Short, every 40-60 seconds in a long video, with the stakes raised every 60-90 seconds. Put a strong point in the middle, where viewers leave most. A minute of pure explanation with no movement loses the audience.
6) THOUGHT + EXAMPLE, always as a pair: every claim gets one concrete, familiar example (a person, an object, a number, a situation from ordinary life). A thought without an example is forgotten; an example without a thought is useless.
7) THE VIEWER, NOT THE AUTHOR: talk about the viewer's life, money, fears and mistakes. The narrator's own story is used only when it is about the viewer too.
8) WRITTEN FOR THE VOICE: short spoken sentences, living words, the reading level of a 12-year-old, no participle chains, no bureaucratic or textbook phrases. Read every line aloud in your head; if the tongue stumbles, rewrite it.
9) NO WATER: cut warm-ups, recaps, "как я уже говорил", filler adjectives, repeated thoughts and dead pauses. Nine dense minutes beat twenty empty ones; a tight 25-second Short beats a loose 45-second one.
10) NO INVENTED FACTS: never invent statistics, studies, quotes or real-world cases presented as true. Use well-known facts, or clearly fictional stories told as stories.
11) THE ENDING: a sharp final thought or punchline that pays off the hook, ideally looping back to the first sentence. Never end on "подписывайся", "на этом всё" or a sign-off. One call to action at most, and only in a long video.
12) LONG VIDEOS: hook (0-20 s), why it matters to the viewer right now, 3-4 blocks each with one thought and one example, spoken transitions between blocks ("с этим разобрались, теперь второе, из-за чего всё ломается"), a short assembly of what to do tomorrow, one action. Never promise "watch to the end, the main thing is there"; people stay because it is interesting now.
DROP-OFF TEST (the editor applies it to every script): read the script as a bored viewer and mark every line where you would leave: predictable, explaining, a warm-up, repeating, abstract without an example, a sign-off. Rewrite or cut each one until none is left."""


SHORT_FIX_EXAMPLE = """

WORKED EXAMPLE OF FIXING A SHORT (learn the technique, never copy the topic or lines):
BEFORE (weak, viewers left at half): "Вчера девушка в такси пять раз написала мужику. Мужик не получил ничего. «Я первой не пишу. Принципиально», говорит. ... Даже холодильник пищит, когда ему нужно внимание. Эти держатся. Скинь другу, который ждёт её сообщения."
Problems: a warm-up first sentence; a confusing line (did she write or not?); a funny situation with no insight; the video ends on a call to action, so viewers swipe before the end.
AFTER: "Ты не пишешь первой не из гордости. Ты боишься. Сейчас докажу. Девушка у меня в такси: «Я первой не пишу. Принципиально». А в зеркале вижу: набрала «привет». Стёрла. Набрала. Стёрла. Пять раз. Спрашиваю: «Страшно?» «Вдруг он поймёт, что нравится». Вот и весь принцип. Это не гордость, это страх отказа в красивой обёртке. А он сидит и думает то же самое. Двое ждут знак, и оба умрут от старости прямо в чате. Даже холодильник пищит, когда ему нужно внимание. Холодильник смелее вас."
Why it works: the first words accuse the viewer and promise proof; the story is the proof; one clear image (typed, deleted); one sharp insight; the last line is a punchline set up one line earlier; about 70 words."""


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


AD_OVERRIDE = """

PRODUCT COMMERCIAL OVERRIDE (this rule beats every instruction above and below about real stories, story engines, heroes, twists, characters and avoiding adverts):
- This channel makes premium product commercials with no people at all. The video IS an advert, and that is the point: it must sell the chosen product beautifully, like a high-end TV spot.
- Only the off-screen narrator speaks. No characters, no dialogue, no people, faces, hands or body parts on screen, ever.
- Structure: a hook in the first 2 seconds (a striking sensory image, a bold promise or a question about a pain or desire); the desire or problem in one or two lines; the reveal of the product; 3-4 benefits, each tied to one thing the camera can show (a macro of the material, the mechanism working, the texture, the detail that makes it better); a feeling of the result; a short call to action (for example: link in the description).
- Language: short, confident, sensory sentences; premium tone, rhythm like a trailer; no lists read aloud, no filler, no clichés like "revolutionary" or "game changer".
- Truth: use only facts from the product's description and photos. Never invent specifications, prices, discounts, awards, certifications, test results or customer reviews.
- Topics and ideas are angles for selling this product (the problem it solves, the moment it shines, the detail nobody notices), not stories about history or people.
- Images: commercial product photography: the product is the hero of every picture with its exact look, studio or stylish set lighting, macro detail, clean backgrounds, dynamic elements (splashes, particles, light streaks, smoke) that fit the product. No people, no hands, no text."""


ONE_TAKE_RULE = """

ONE TAKE (this beats every rule above and below about locations, dynamic elements and changing scenes):
- The whole video is one continuous camera move around the product on ONE set in ONE location, like a motion-control commercial shot in one take. The set never changes: the same surface or pedestal, the same background, the same light and the same few props in every picture.
- In the outline give exactly one location: the set, described precisely (surface, background colour and texture, light direction, 1-3 matching props) so it can be drawn the same every time.
- The product stands still in one spot the whole time; only the camera moves: it pushes in, circles, rises, dives into a macro detail, pulls back. No splashes, no levitation, no new objects, no jumps to other places."""


MUTE_OVERRIDE = """

NO VOICE (this beats every rule above and below about the narrator, speech, voice, word counts and length):
- Nobody speaks in this video and no text is shown: it runs to music only, the pictures tell everything.
- The script is only the plan of the shots: about {lines} lines, one per paragraph (separate lines with a blank line), each 3-8 words saying what this moment shows and makes the viewer feel ("The blade catches the light", "Slow reveal of the gold edge").
- The lines still follow the structure above (hook, desire, reveal, benefits the camera shows, final hero shot), one line per shot."""


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


FRAMING_OPENERS = ("вчера", "сегодня", "однажды", "как-то", "как то", "вёз я", "вез я", "еду я", "знаете", "короче,",
                   "итак", "расскажу", "сейчас расскажу", "давайте", "one day", "yesterday", "today i", "so,",
                   "let me tell", "i want to tell", "have you ever wondered")
SIGN_OFFS = ("подпиш", "подписыва", "скинь", "перешли", "отправь друг", "поделись", "ставь лайк", "поставь лайк",
             "поехали дальше", "на этом всё", "на этом все", "вот такие дела", "пиши в комментари", "пишите в комментари",
             "subscribe", "like and", "share this", "send this to", "see you", "comment below", "that's all")


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
    first = (sentences[0] if sentences else "").lower().lstrip("«\"'— ")
    framing = [p for p in FRAMING_OPENERS if first.startswith(p)]
    tail = " ".join(sentences[-4:]).lower()[-260:]
    bad_ending = [p for p in SIGN_OFFS if p in tail]
    # Chopped "telegraph" text (many 2-4 word sentences) is hard to follow by ear.
    avg = round(wc / len(sentences), 1) if sentences else 0
    return {
        "avg_sentence": avg,
        "choppy": bool(sentences) and avg < 6.5 * wpm / WORDS_PER_MIN,  # Russian sentences have fewer words
        "word_count": wc,
        "minutes": round(wc / wpm, 1),
        "target_words": target_words,
        "length_ok": abs(wc - target_words) <= target_words * 0.15,
        "longest_sentence": longest,
        "long_sentences": long_sentences,
        "first_sentence": sentences[0] if sentences else "",
        "bad_openers": bad_openers,
        "framing_opener": framing,  # a Short must open with the hook itself, not "yesterday a passenger..."
        "bad_ending": bad_ending,  # a Short must end on the punchline, not on a call to action or a sign-off
    }


def check_problems(checks: dict, short: bool) -> list:
    """The automatic checks the editor's text failed, in words the editor model acts on."""
    out = []
    if checks.get("bad_openers"):
        out.append(f"the opening greets the viewer ({', '.join(checks['bad_openers'])}): start with the hook itself")
    if short and checks.get("framing_opener"):
        out.append(f"the first words are a framing warm-up (\"{checks['first_sentence'][:80]}\"): the first sentence "
                   "must be the sharpest claim or accusation aimed at the viewer; the story comes after it as proof")
    if short and checks.get("bad_ending"):
        out.append(f"the ending is a call to action or a sign-off ({', '.join(checks['bad_ending'])}): cut it, the "
                   "last line must be the punchline")
    if not checks.get("length_ok"):
        out.append(f"length is {checks.get('word_count')} words, the target is about {checks.get('target_words')}")
    return out


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

AD_IDEAS_EXTRA = """

PRODUCT COMMERCIAL SHORTS: every idea is a different angle for selling the chosen product (the pain it ends, the moment it shines, the hidden detail, the before and after, the feeling it gives). For every idea also give first_line (the exact first spoken sentence, in the video's language), twist (the wow moment of the spot: the shot or fact that makes people want it) and twist_ru (the same in Russian). No people in any idea."""

IDEA_CRITIC = """You are a jaded 20-year-old who scrolls YouTube Shorts for hours and swipes away from anything boring, predictable or advert-like within 2 seconds. You are judging ideas for one channel.

For every idea score from 1 to 10, honestly and harshly:
- stop_scroll: would the first line make you stop swiping?
- unpredictable: after the first line, could you guess the ending? (10 = no way, 1 = obvious)
- payoff: would the twist and the last line make you laugh, gasp or replay?
Ideas that read like an advert or where the hero simply wins the obvious way get at most 3 for unpredictable.
Judge as this channel's real audience described below, not by your own taste: an idea that breaks the channel's niche or the owner's rules is worth nothing however catchy it is.
Add comment_ru: one short honest sentence in Russian.
Then choose the idea you would really watch to the end (chosen_index, counting from 0) and explain in reason_ru in 2 sentences in Russian.

CHANNEL (the owner's description, may be in Russian):
{niche}{audience}

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
        "thumbnail_prompt": {"type": "string"}, "thumbnail_font": {"type": "string"}, "thumbnail_look": {"type": "string"},
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
            if self.opts.get(k) not in (None, ""):  # the montage page sets the voice for one video
                self.settings[k] = float(self.opts[k])
        if self.opts.get("tts_model"):
            self.settings["elevenlabs_model"] = self.opts["tts_model"]
        self.lang = self.opts.get("language") or self.project.get("language") or "en"
        self.characters = read_json(self.dir / "characters.json", []) or []
        self.items = read_json(self.dir / "items.json", []) or []
        if self.project.get("ad_mode"):
            self.characters = []  # a product commercial shows no people
        host = self.host()
        if host:  # the hero tells the whole video in his own voice
            self.settings["elevenlabs_voice_id"] = host["voice_id"]
            self.settings["elevenlabs_voice_name"] = host.get("voice_name", "")
        if self.opts.get("voice_id"):  # a translated version: the owner chose its voice separately
            self.settings["elevenlabs_voice_id"] = self.opts["voice_id"]
            self.settings["elevenlabs_voice_name"] = self.opts.get("voice_name", "")
        self.wpm = measured_wpm(self.pace_key()) or WPM.get(self.lang, WORDS_PER_MIN)
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

    @property
    def one_take(self) -> bool:
        """A product commercial shot as one continuous camera move around the product on one set."""
        return bool(self.project.get("ad_mode") and self.project.get("one_take"))

    def take_shots(self) -> int:
        """One take: how many shots fit the video when every clip lasts its full 5 seconds."""
        seconds = self.short_seconds if self.short_format else self.minutes * 60
        return max(2, round(seconds / TAKE_CLIP))

    @property
    def mute(self) -> bool:
        """No voice: the video goes to music only, its text is shown on screen."""
        return bool(self.opts.get("voice_off"))

    def voice_tempo(self) -> float:
        """How much faster the speech is played than ElevenLabs said it (its own speed stops at 1.2)."""
        try:
            t = float(self.opts.get("voice_tempo") or self.project.get("voice_tempo") or 1)
        except (TypeError, ValueError):
            t = 1.0
        return min(TEMPO_MAX, max(TEMPO_MIN, t))

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
                          + (f"Owner's description, the main source about who this character is and what he talks "
                             f"about (Russian): {ch['description_ru']} " if (ch.get("description_ru") or "").strip() else "")
                          + (f"(Older automatic summary, the owner's description and manner of speech win where they "
                             f"differ: {ch.get('personality', '')}) " if ch.get("personality") and (
                                 (ch.get("description_ru") or "").strip() or (ch.get("speech_ru") or "").strip())
                             else f"{ch.get('personality', '')} ")
                          + f"Looks: {ch.get('appearance', '')}"
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
                          + (f" What it is and how it is used (owner, may be in Russian): {it['description_ru']}" if it.get("description_ru") else "")
                          + (f" Owner's notes, follow them (may be in Russian): {it['extra_ru']}" if it.get("extra_ru") else "")
                          for it in self.items)
        return ("\n\nITEMS AND PRODUCTS FOR THIS VIDEO (the owner chose them, they have exact reference photos):\n"
                + lines + "\n"
                + ("- This is the product the commercial sells. It is in every picture on its own, never held, worn or used "
                   "by a person. Call it by exactly this name; never invent other brands or models.\n"
                   if self.project.get("ad_mode") else
                   "- Let these items appear in the story and in several pictures where they fit naturally: used, held, "
                   "worn, standing in the scene. Call them by exactly these names; never invent other brands or models "
                   "of the same kind.\n")
                + "- In image prompts describe every such item with its exact look above; never change its shape, colours, "
                "logo or labels.")

    def host(self):
        """In "host" mode the first chosen character with a voice tells the whole video himself."""
        if self.project.get("voice_mode") != "host" or self.opts.get("voice_id"):
            return None
        return next((ch for ch in self.characters if ch.get("voice_id")), None)

    def strip_host_labels(self, script: str) -> str:
        """In host mode the hero tells everything himself: a stray "Исмаил:" before a line would be read aloud."""
        host = self.host()
        if not host:
            return script
        names = [re.escape(n.strip()) for n in (host.get("name"), host.get("name_en")) if n and n.strip()]
        if not names:
            return script
        return re.sub(r"(?im)^[ \t]*[*_]*(?:" + "|".join(names) + r")[*_]*[ \t]*:[ \t]*", "", script)

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
            recap = ("No recap: open right on the consequence of the previous episode's ending." if self.short_format else
                     "After the hook (not before it), give a very short recap of the previous episode in 1-2 "
                     "sentences, then pick up its ending: answer or develop the hook it left.")
        elif self.episode > 1:
            recap = ("Earlier episodes are not recorded here: continue the story naturally from the series bible, "
                     "without retelling or contradicting it.")
        else:
            recap = ("This is the first episode: introduce the hero, the world and the main conflict of the season "
                     "quickly through action, not exposition.")
        plan = projects.plan_line(self.project, self.episode)
        if plan:
            parts.append(f"OWNER'S PLAN FOR THIS EPISODE (may be in Russian): {plan}")
        parts.append(SERIES_RULES.format(num=self.episode, recap=recap).strip())
        return "\n\n".join(parts)

    def pace_key(self) -> str:
        """The voice's pace depends on the language, the voice and its speed setting."""
        return ":".join(str(x) for x in (self.lang, self.settings.get("elevenlabs_voice_id") or "",
                                          self.settings.get("voice_speed") or ""))

    def history_block(self) -> str:
        """Ideas of this project's earlier videos, so new ones do not repeat them."""
        pid = self.project.get("id")
        if not pid:
            return ""
        seen = []
        for j in sorted(db.list_all(), key=lambda j: j["id"], reverse=True):
            if j["id"] == self.id or (j.get("options") or {}).get("project_id") != pid \
                    or (j.get("options") or {}).get("translate_from"):
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
            if (any((ch.get("speech_ru") or "").strip() for ch in self.characters) or self.short_seconds < 45
                    or self.project.get("made_for_kids") or self.project.get("fiction")):
                # The built-in samples are calm science explainers: next to a character with his own manner
                # they pull the script towards a textbook voice and textbook topics.
                return ""
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

    def library_block(self, prompt_file: str) -> str:
        """The niche's script library (analyses of videos that worked, saved by the AI producer): its playbook and
        the newest analyses of the same format."""
        if prompt_file not in GOLDEN_STAGES or not self.project.get("id") or self.opts.get("translate_from"):
            return ""
        from . import projects
        lib = projects.library(self.project["id"])
        kind = "short" if (self.short_format or prompt_file == "07_short.txt") else "video"
        items = [x for x in lib["items"] if x.get("kind") in (kind, "channel")][-LIBRARY_IN_PROMPT:]
        rules = lib.get("playbook") or lib.get("playbook_ru") or ""
        if not (items or rules):
            return ""
        text = ("\n\nSCRIPT LIBRARY OF THIS NICHE (analyses of competitors' and the owner's videos that got many views; "
                "may be in Russian). Study why they worked and use the same techniques; never copy their topics, plots, "
                "jokes or lines.")
        if rules:
            text += "\nPLAYBOOK OF THE NICHE:\n" + rules[:3000]
        parts = []
        for x in reversed(items):
            head = f"### {x.get('title', '')}" + (f" ({x['views']} views)" if x.get("views") else "")
            row = [head, f"Why it worked: {x.get('why_ru', '')}"]
            if x.get("hook_ru"):
                row.append(f"Hook: {x['hook_ru']}")
            if x.get("techniques"):
                row.append("Techniques: " + "; ".join(x["techniques"][:10]))
            if x.get("avoid_ru"):
                row.append("Avoid (this is what made the weak videos fail): " + "; ".join(x["avoid_ru"][:6]))
            if prompt_file == "01_ideas.txt" and x.get("topics_ru"):
                row.append("Topic ideas in this spirit: " + "; ".join(x["topics_ru"][:8]))
            if prompt_file in ("03_script.txt", "07_short.txt") and x.get("script"):
                row.append("Opening of its script: " + x["script"][:700 if kind == "short" else 900])
            parts.append("\n".join(row))
        if parts:
            text += "\n<<<\n" + "\n\n".join(parts) + "\n>>>"
        return text

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
                "for word; write new lines in the same manner. His slang is allowed and wanted, but no hard swearing. "
                "This voice rule beats the generic writing rules of the task (for example \"use second person "
                "sparingly\" or sensory description). A short scene from the character's own life (a passenger, "
                "a day at work) is allowed as his way of telling, when his description or the niche suggests it; "
                "facts presented as real stay true."
                + (" In a Short his story never opens the video: the first words are the hook aimed at the viewer, the "
                   "passenger scene comes right after as proof; and his pet phrases never come after the punchline as a "
                   "sign-off." if self.short_format or prompt_file == "07_short.txt" else ""))
        if prompt_file == "01_ideas.txt":
            text += ("\nTopics come from this character's world as the owner described him: everyday, relatable things "
                     "any viewer knows from their own life and that fit his job and life as described, seen through his eyes and told as his bitter truth or as a lie he exposes. "
                     "No textbook science, history lessons or trivia quizzes: a fact is only a tool for his point.")
        if prompt_file == "04_review.txt":
            text += ("\nAdd the criterion voice: does the script sound like this character (10) or like a neutral "
                     "narrator (at most 4)? Below 7 means revised_script must be rewritten in his voice. "
                     "Never smooth his slang or pet words out when you polish.")
        return text

    def tiny_short(self) -> str:
        """A Short of 30 seconds or less has room for only 3-4 sentences: the full engine does not fit."""
        if self.short_seconds > 30:
            return ""
        return (f"\n\nVERY SHORT SHORT ({self.short_seconds} seconds, about {self.words} words, 3-4 sentences): this beats the "
                "story engine and the series rules above. Hook, one turn (the common belief broken), punchline. The answer may "
                "come in the third sentence. No escalation stage, no recap, no subscribe line; in a series the punchline "
                "itself is the hook for the next episode.")

    def ask(self, prompt_file: str, schema: dict, max_tokens: int = 32000, extra: str = "", **values) -> dict:
        lang = LANG_OVERRIDE.get(self.lang, "")
        if self.short_format:  # the line about the extra Short is for long videos only
            lang = "\n".join(x for x in lang.split("\n") if not x.startswith("- The Short is "))
        system = (fill(load_prompt("00_system.txt"), niche=self.niche) + lang
                  + (SHORT_OVERRIDE.format(seconds=self.short_seconds, words=self.words) + self.tiny_short()
                     if self.short_format else "")
                  + (KIDS_OVERRIDE if self.project.get("made_for_kids")
                     else FICTION_OVERRIDE if self.project.get("fiction") else "")
                  + (AD_OVERRIDE if self.project.get("ad_mode") else "")
                  + (ONE_TAKE_RULE if self.one_take else "")
                  + (MUTE_OVERRIDE.format(lines=self.take_shots() if self.one_take
                                          else max(5, round(self.short_seconds / 3.2)) if self.short_format
                                          else max(8, round(self.minutes * 60 / 3.5))) if self.mute else "")
                  + self.char_block() + self.item_block() + self.dialogue_block() + self.series_block() + self.examples_block()
                  + self.lessons_block() + self.golden_block(prompt_file) + self.library_block(prompt_file)
                  + (SCRIPT_CRAFT if prompt_file in GOLDEN_STAGES else "")
                  + (SHORT_FIX_EXAMPLE if (self.short_format and prompt_file in GOLDEN_STAGES) or prompt_file == "07_short.txt" else "") + OWNER_FIRST
                  + (OWNER_TOPIC.format(topic=own_topic(self).replace("{", "(").replace("}", ")"))
                     if prompt_file in GOLDEN_STAGES and own_topic(self) else ""))
        length = f"{self.short_seconds}-second" if self.short_format else f"{max(1, int(self.minutes))}-minute"
        user = fill(load_prompt(prompt_file), niche=self.niche, minutes=max(1, int(self.minutes)), length=length,
                    words=self.words,
                    words_min=int(self.words * 0.9), words_max=int(self.words * 1.1), **values) + extra \
            + self.voice_rule(prompt_file)
        result = llm.call_json(self.settings, system, user, schema, self.usage, max_tokens)
        self.save_usage()
        return result


# ---------------------------------------------------------------- stages

GOLDEN_STAGES = ("01_ideas.txt", "02_outline.txt", "03_script.txt", "04_review.txt", "07_short.txt")
GOLDEN_IN_PROMPT = 6
LIBRARY_IN_PROMPT = 5
POSITIONS_RULE = (
    "\n\nPOSITIONS (the image model gets each prompt alone and guesses where people are): in every prompt say "
    "exactly where each person is and what they do there, in the subject part, not only in the location text. "
    "In a vehicle always name the seat: \"the woman sits in the REAR passenger seat\", \"the driver at the wheel\". "
    "A person alone in a car is drawn behind the wheel unless told otherwise, so for a passenger frame the shot "
    "from the front seats looking back at the rear seat, keep the steering wheel out of the passenger's part of "
    "the picture and write \"she is not driving\". The same for a counter, a desk or a stage: who stands behind "
    "it and who in front. Never solve this by emptying a place that the story fills: in a moving car the driver is "
    "always at the wheel, so for a passenger frame show the driver's shoulder, hands on the wheel or eyes in the "
    "mirror, or frame so the front seat is out of the picture; never write \"the driver's seat is empty\".")
STORY_LOGIC = (
    "\n\nSTORY LOGIC (every picture is checked against it; a picture that breaks it is a failure):\n"
    "- Each picture shows the same world as the story at that moment: who is where, what is happening, what time "
    "it is. A taxi that is driving has its driver at the wheel; a conversation has both people present; a scene "
    "that continues keeps the same people, clothes, light and weather.\n"
    "- Never invent absences, extra people, impossible states or things the narration does not imply (an empty "
    "driver's seat in a moving car, a stranger in the back seat, daylight in a night scene, a third hand).\n"
    "- A recurring character who is physically there and partly visible (back of the head, hands on the wheel, "
    "eyes in the mirror) is listed in characters, so his look stays the same.\n"
    "- If the given camera angle is unnatural in this place (a top-down view inside a car), use the nearest "
    "natural angle that still differs from the neighbouring pictures.\n"
    "- Symbols and metaphors (a gift box for \"страх в красивой обёртке\") are fine, but stay in the scene's world "
    "or clearly cut away to them; never mix them into a literal scene in a way that makes no sense.")
OWNER_FIRST = ("\n\nPRIORITY: the channel niche above and the characters' descriptions and manner of speech are the "
               "owner's own rules. Where they differ from the general rules of this prompt (topics, tone, audience, "
               "structure, how the narrator talks), the owner's rules win.")
VOICE_STAGES = ("01_ideas.txt", "02_outline.txt", "03_script.txt", "04_review.txt", "07_short.txt")


def ideas_count(settings: dict) -> int:
    try:
        return max(3, min(int(float(settings.get("ideas_count") or 5)), 10))
    except (TypeError, ValueError):
        return 5


def own_topic(c: Ctx) -> str:
    """The topic the owner wrote himself ("" when the program chooses, and for series episodes)."""
    return "" if c.episode else (c.job.get("topic") or "").strip()


OWNER_TOPIC = (
    "\n\nOWNER'S TOPIC (beats every other rule here, including the niche's topic rules and the characters):\n"
    "The owner chose this video's topic himself: \"{topic}\"\n"
    "The video is about exactly this topic: same subject, same angle, every point the owner named. Never replace "
    "it, never drift to a neighbouring subject, never twist its meaning. Characters only add their voice, manner "
    "and reactions on top of this topic and script; they never change what the video is about. All the craft "
    "rules apply only to make THIS topic interesting.")


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
        block = (f"The channel owner chose the topic himself (it may be written in Russian):\n\"{topic}\"\n"
                 "Give exactly ONE idea: the owner's topic itself, exactly as written. Do not change its subject, "
                 "angle, claim, heroes or any point the owner named, do not replace it with a 'better' or "
                 "neighbouring topic and do not add a new plot. Your job is only to package it: a title, the "
                 "premise in the owner's own meaning, a hook and the key facts that serve this exact topic.")
    else:
        block = ("Choose freely within the channel niche and follow its rules about topics exactly. Make the ideas "
                 "diverse within those rules.")
    # The owner's own topic is taken even if it resembles an earlier video: he chose it knowingly.
    return block + ("" if own_topic(c) else c.history_block())


def _ask_ideas(c: Ctx, n: int, avoid: list = None) -> dict:
    extra = (AD_IDEAS_EXTRA if c.project.get("ad_mode") else SHORT_IDEAS_EXTRA) if c.short_format else ""
    extra += f"\n\nGive exactly {n} ideas, no more and no fewer."
    if not c.short_format:
        extra += (f" This video will be about {max(1, round(c.minutes))} minutes long: a title that names its length "
                  "uses exactly that number of minutes, never another one.")
    if own_topic(c) and avoid:  # the owner pressed "more topics" himself
        extra += ("\n\nThe owner gave a topic: variety only inside that topic, never leave it for another subject. "
                  "The owner's topic itself is already in the list, so give new close variations of it.")
    if avoid:
        extra += ("\n\nThese ideas were already proposed for this video and the owner wants NEW ones: no repeated "
                  "stories, situations or twists:\n" + "\n".join(f"- {t}" for t in avoid))
    return c.ask("01_ideas.txt", SCHEMA_IDEAS, 16000, extra=extra, topic_block=_idea_block(c), count=n)


def st_idea(c: Ctx):
    n = 1 if own_topic(c) else ideas_count(c.settings)
    if c.demo:
        r = _demo("idea")
    else:
        c.log("Оформляю вашу тему, другие темы не придумываю…" if n == 1 else f"Придумываю {n} тем и выбираю лучшую…")
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
    if own_topic(c):
        ideas[:] = ideas[:1]
        idx = 0
        c.log("Беру вашу тему как есть, без других вариантов.")
    elif c.short_format and not c.demo and len(ideas) > 1:
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
                           IDEA_CRITIC.format(ideas=text, niche=c.niche.strip()[:3000], audience=(
                               "\nThe audience is small children and their parents: kind, safe, happy endings are required; "
                               "judge surprise by the unexpected way the story gets there." if c.project.get("made_for_kids") else
                               "\nThis channel makes product commercials, so an advert is expected here: ignore the rule above about adverts and judge "
                               "whether the first line and the wow moment would stop your scroll and make you want the product."
                               if c.project.get("ad_mode") else "")), SCHEMA_IDEA_CRITIC, c.usage, 6000)
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
        r = c.ask("02_outline.txt", SCHEMA_OUTLINE, 32000, idea=text, extra=_saved_places_block(c))
    write_json(c.out("outline"), r)


def _saved_places_block(c: Ctx) -> str:
    """A series keeps its places: the writer reuses their exact names, so the saved pictures are found again."""
    if not (c.project.get("keep_places") and c.project.get("id")):
        return ""
    from . import projects
    saved = projects.list_places(c.project["id"])
    if not saved:
        return ""
    return ("\n\nSAVED PLACES OF THIS SERIES (already drawn in earlier episodes). When the story happens in one of "
            "them, list it in locations with EXACTLY this name and visual, character for character; invent a new "
            "place only when the story really goes somewhere else:\n"
            + "\n".join(f"- {x['name']}: {x.get('visual', '')}" for x in saved))


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


REVIEW_FOCUS = (
    "\n\nHOW TO REWRITE (beats the instructions above): change only what fixes a listed problem and keep every "
    "sentence that already works word for word, so a fix in one place never breaks another. Fix the lowest "
    "criteria first. If the hook promises something the story never delivers (\"why you still won't sleep "
    "well\"), either deliver it clearly in the ending or change the hook so it promises exactly what the story "
    "pays off: the last paragraph must answer the hook's question in plain words.")
EDITOR_GATES = (
    "\n\nHARD GATES (check each one before scoring; a failed gate caps the named score at 4, whatever else is good):\n"
    "1) HOOK GATE: the first sentence is the sharpest claim, accusation, number or absurd fact of the video, aimed at "
    "the viewer. A warm-up (\"Вчера девушка в такси...\", \"Сегодня расскажу\", \"Однажды\", a name, the weather) "
    "fails: hook <= 4.\n"
    "2) PROMISE GATE: the hook opens a question or makes a claim, and the ending answers or proves it in plain words. "
    "Unanswered or replaced by another point: payoff <= 4.\n"
    "3) LISTEN-ONCE GATE: every line is understood on the first listen with eyes closed. A line that can be read two "
    "ways (\"она пять раз написала, он ничего не получил\" when she typed and deleted) fails: clarity <= 4.\n"
    "4) ENDING GATE (Shorts): the last line is the punchline. Any call to action or sign-off at the end (\"скинь "
    "другу\", \"подписывайся\", \"поехали дальше\") fails: payoff <= 4. The share request belongs in the description.\n"
    "5) ONE-THOUGHT GATE: the whole video serves one thought you can say in one sentence; a second topic fails: "
    "pacing <= 4.\n"
    "6) INSIGHT GATE (channels about psychology, money, relationships, advice): at least one sharp line names what is "
    "really going on (\"это не гордость, это страх отказа в красивой обёртке\"). Only a funny situation with no "
    "insight: surprise <= 5.\n"
    "7) WATER GATE: no sentence that only repeats, explains the joke or recaps. Each one found: pacing -1.\n"
    "8) OWNER GATE: the owner's topic, niche rules and the characters' manner of speech are followed exactly; any "
    "conflict: voice or clarity <= 4.\n"
    "Name every failed gate in issues_ru.")
SCORE_ONLY = (
    "\n\nTHIS IS THE FINAL CHECK, SCORE ONLY: do not rewrite. Return revised_script as an empty string. "
    "issues_ru and verdict_ru describe only this exact text as it is now; never write that you fixed, "
    "strengthened or changed anything.")


def _review_history(rounds: list) -> str:
    """What earlier rounds already found, so the next rewrite does not bring old problems back."""
    if not rounds:
        return ""
    lines = []
    for i, r in enumerate(rounds, 1):
        weak = [f"{s.get('criterion')} {s.get('score')}" for s in r.get("scores", []) if int(s.get("score", 10)) < 8]
        lines.append(f"Round {i}: {r.get('overall')}/10; weak: {', '.join(weak) or 'none'}; problems: "
                     + "; ".join((r.get("issues_ru") or [])[:8]))
    return ("\n\nEARLIER EDITOR ROUNDS (the script below is already their rewrite). Problems found before must "
            "not come back, and criteria that stayed low round after round need a real change, not polish:\n"
            + "\n".join(lines))


def _evaluate(c: Ctx, script: str, round_no: int, history: list = None, score_only: bool = False) -> dict:
    if c.demo:
        r = _demo("review", script=script)
    else:
        c.log(f"Редактор: раунд {round_no}, {'финальная оценка' if score_only else 'проверяю по чек-листу'}…")
        failed = check_problems(script_checks(script, c.words, c.wpm), c.short_format)
        extra = (_review_history(history or []) + EDITOR_GATES
                 + ("\n\nAUTOMATIC CHECKS FAILED ON THIS TEXT (each one caps the matching score at 4 and must be fixed "
                    "in revised_script):\n- " + "\n- ".join(failed) if failed else "")
                 + (SCORE_ONLY if score_only else REVIEW_FOCUS))
        r = c.ask("04_review.txt", SCHEMA_REVIEW, 8000 if score_only else 32000, extra=extra,
                  outline=_outline_text(c), script=script, word_count=word_count(script))
        if score_only:
            r["revised_script"] = ""
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
        and not r["checks"]["bad_openers"] \
        and not (c.short_format and (r["checks"]["framing_opener"] or r["checks"]["bad_ending"])) \
        and not (r["checks"].get("choppy") and not c.short_format and not c.voice_rule("04_review.txt"))
    # Short punchy lines are the point of a Short and of a host's own manner of speech, so they are not a fault there.
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
        r = _evaluate(c, current, len(rounds) + 1, rounds)
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
            last = _evaluate(c, current, len(rounds) + 1, rounds, score_only=True)
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
    (d / "improve.json").unlink(missing_ok=True)
    reset_from(jid, "scenes", keep_pictures=True)


SCHEMA_IMPROVE = {"type": "object", "properties": {
    "summary_ru": {"type": "string"},
    "edits": {"type": "array", "items": {"type": "object", "properties": {
        "op": {"type": "string", "enum": ["replace", "insert_after", "delete"]}, "index": {"type": "integer"},
        "text": {"type": "string"}, "text_ru": {"type": "string"}, "why_ru": {"type": "string"}},
        "required": ["op", "index", "text", "text_ru", "why_ru"]}},
    "titles": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "title_ru": {"type": "string"}}, "required": ["title", "title_ru"]}},
    "thumbnail_texts": {"type": "array", "items": {"type": "string"}}},
    "required": ["summary_ru", "edits", "titles", "thumbnail_texts"]}

IMPROVE_PROMPT = """Below is the finished narration of a video, split into numbered paragraphs. It is already good: most of it must stay exactly as it is, because every changed paragraph means new paid pictures. Find ONLY its weak spots against the rules of this niche and the analyses of videos that worked (in the system prompt), and fix just those.

Weak spots worth fixing: a hook that does not open with the viewer's own situation and stakes; a question or promise opened and never answered; a myth that is stated but not overturned with a reveal; a chapter without a "wow" fact or without an open loop to the next one; numbers that are not translated into something the viewer feels; a payoff that is announced ("rewrites the whole story") but not delivered; an ending that does not close the loop or ends on a weak call to action instead of a question for the comments; parts that drag. If the text is shorter than the target, you may add 1-3 new paragraphs (insert_after) where they make the video stronger.

Rules:
- Change at most a third of the paragraphs. Do not touch paragraphs that already work, do not polish wording for its own sake.
- Keep every fact, name, number and the owner's caveats. Never invent new facts, studies or numbers: a new paragraph may only use facts that are already in the text or common knowledge.
- Write in the language of the script, in the same voice, short spoken sentences.
- op "replace": index = the paragraph to rewrite, text = the whole new paragraph. "insert_after": text is a new paragraph placed after paragraph index (0 = at the very beginning). "delete": remove paragraph index (text empty).
- text_ru: the new text in Russian for the owner (if the script is Russian, the same text). why_ru: in Russian, one or two sentences: what was weak and which rule of the niche the fix follows.
- summary_ru: in Russian, 2-3 sentences: the overall verdict and what the edits change.
- titles: 3 title ideas in the script's language that follow the niche rules, with Russian translations. thumbnail_texts: 3 thumbnail texts of 2-3 words in the script's language.
{owner}
Target length: about {target} words, now {now} words.
Current title: {title}
Current thumbnail text: {thumb}

THE NARRATION:
{paragraphs}"""


def improve_script(jid: str, note: str = "") -> dict:
    """The writer fixes only the weak spots of the final text, measured against the niche rules and the library.
    The owner then picks which edits go into the text; unchanged paragraphs keep their pictures."""
    job = db.get(jid)
    d = job_dir(jid)
    review = read_json(d / "review.json")
    if not job or not review:
        raise UserError("Текст ещё не готов: дождитесь шага «Проверка редактором».")
    c = Ctx(job)
    paras = [p.strip() for p in re.split(r"\n\s*\n", review["final_script"]) if p.strip()]
    extras = read_json(d / "extras.json") or {}
    thumb = (read_json(d / "thumb.json") or {}).get("text") or extras.get("thumbnail_text", "")
    system = ("You are the head writer of a YouTube channel who edits finished scripts with a light hand. "
              f"Channel niche: {c.niche}" + c.lessons_block() + c.library_block("03_script.txt"))
    owner = f"\nTHE OWNER'S NOTES (follow them first; they may be in Russian):\n{note.strip()[:4000]}\n" if note.strip() else ""
    user = IMPROVE_PROMPT.format(owner=owner, target=c.words, now=word_count(review["final_script"]),
                                 title=extras.get("youtube_title") or (c.data("outline") or {}).get("title", ""),
                                 thumb=thumb or "-", paragraphs="\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(paras)))
    r = llm.call_json(c.settings, system + "\nAnswer only with JSON.", user, SCHEMA_IMPROVE, c.usage, 16000)
    c.save_usage()
    edits = []
    for e in r.get("edits") or []:
        i = int(e.get("index") or 0)
        if e.get("op") == "insert_after" and 0 <= i <= len(paras) and e.get("text", "").strip():
            edits.append({**e, "anchor": paras[i - 1] if i else "", "old": ""})
        elif e.get("op") in ("replace", "delete") and 1 <= i <= len(paras) and (e["op"] == "delete" or e.get("text", "").strip()):
            edits.append({**e, "old": paras[i - 1]})
    r["edits"] = edits
    r["at"] = time.time()
    write_json(d / "improve.json", r)
    return r


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


# Product commercials: (key, picture, camera move for the animated clip, still-picture move).
AD_SHOTS = [
    ("macro", "extreme macro close-up of the product's surface and material, razor-thin depth of field, one detail in sharp focus",
     "slow macro slide across the surface with a gentle focus pull", "pan_right"),
    ("hero", "hero shot: the product alone in the center on a pedestal, slightly low angle, dramatic rim light, clean gradient background",
     "slow dolly push-in toward the product", "zoom_in"),
    ("orbit", "three-quarter view of the product on a turntable, soft studio light with glossy reflections",
     "smooth orbit of the camera around the product", "pan_left"),
    ("top", "top-down flat lay: the product among a few matching props on a styled surface, no people",
     "slow top-down rotation with a gentle push-in", "zoom_in"),
    ("float", "the product levitating in mid-air, slightly tilted, small particles floating around it",
     "the product slowly turns in mid-air while particles drift", "zoom_out"),
    ("splash", "the product with a frozen dynamic element of its world around it (water splash, powder burst, sparks, petals or smoke, whatever fits it)",
     "slow-motion burst around the product while the camera holds steady", "zoom_in"),
    ("detail", "extreme close-up of one functional detail of the product (button, blade, cap, nozzle, logo), shallow depth of field",
     "slow macro push-in on the detail with a rack focus", "zoom_in"),
    ("reveal", "dark moody scene, the product half in shadow, carved out by a single sweeping streak of light",
     "a light sweep slowly reveals the product while the camera pulls back", "zoom_out"),
    ("place", "wide shot of the product in a stylish real setting where it is used (shelf, desk, bathroom, kitchen), no people",
     "slow pull-back that reveals the setting", "zoom_out"),
]
AD_MOVE = {k: m for k, _, m, _ in AD_SHOTS}

# One take: a camera path around the product on one set; each framing is a short move from the previous one.
# (key, framing, the move from this framing to the next one, still-picture move)
TAKE_SHOTS = [
    ("wide", "front view at eye level: the product standing upright in the middle of the set and filling about 60% of "
             "the frame height, the whole product tack-sharp from top to bottom with every detail and texture crisp, "
             "the set around it visible",
     "slow dolly push-in toward the product", "zoom_in"),
    ("push", "medium shot from the front, the camera moved closer: the product fills about half of the frame",
     "the camera glides closer while circling to the left", "zoom_in"),
    ("left", "close three-quarter view from the front-left: the camera has circled to the left of the product",
     "the camera pushes into an extreme macro of the key detail", "zoom_in"),
    ("detail", "extreme macro close-up of the product's key functional detail seen from the front-left, very shallow depth of field, the set softly blurred behind",
     "the camera pulls back from the macro and keeps circling to the side", "zoom_out"),
    ("side", "close side view in profile from the left: the camera keeps circling around the product",
     "the camera circles around the front to the right side", "pan_right"),
    ("right", "medium three-quarter view from the front-right: the camera has circled to the right side",
     "the camera rises and tilts down over the product", "pan_left"),
    ("top", "high angle from above and slightly in front, looking down at the product in its spot on the set",
     "the camera cranes down to a low angle in front of the product", "zoom_out"),
    ("hero", "low-angle hero shot from the front, a little wider: the product towers over the set with a rim light",
     "very slow push-in toward the product", "zoom_in"),
]
TAKE_MOVE = {k: m for k, _, m, _ in TAKE_SHOTS}


TAKE_CLIP = 5.0  # seconds of one Kling clip: one take shows each clip whole, at its own speed


def take_chunks(chunks: list, n: int) -> list:
    """Merges neighbouring pieces of the script into n scenes: one scene per 5-second clip."""
    if len(chunks) <= n:
        return chunks
    out = []
    for k in range(n):
        part = chunks[round(k * len(chunks) / n):round((k + 1) * len(chunks) / n)]
        out.append({**part[0], "text": " ".join(x["text"] for x in part), "paragraph_end": part[-1]["paragraph_end"]})
    return out


def take_plan(n: int) -> list:
    """The camera path for n pictures: starts wide, ends on the hero shot, the middle loops around the product."""
    keys = [k for k, *_ in TAKE_SHOTS]
    if n <= 1:
        return ["hero"][:n]
    short_paths = {2: ["wide", "hero"], 3: ["wide", "detail", "hero"], 4: ["wide", "push", "detail", "hero"],
                   5: ["wide", "push", "detail", "side", "hero"], 6: ["wide", "push", "left", "detail", "right", "hero"],
                   7: ["wide", "push", "left", "detail", "side", "top", "hero"]}
    if n in short_paths:  # short videos keep the push-in and the macro detail, the heart of a product spot
        return short_paths[n]
    if n <= len(keys):
        return keys[:n - 1] + ["hero"]
    middle = keys[1:-1]
    return ["wide"] + [middle[i % len(middle)] for i in range(n - 2)] + ["hero"]


def ad_shot_plan(n: int, seed: str) -> list:
    """Opens on a macro tease, ends on the hero packshot, never repeats a neighbour in between."""
    import random
    rnd = random.Random(seed)
    keys = [k for k, *_ in AD_SHOTS]
    plan = []
    for i in range(n):
        if i == 0:
            plan.append("macro")
        elif i == n - 1 and plan[-1] != "hero":
            plan.append("hero")
        else:
            avoid = set(plan[-2:]) | {"hero"}  # the packshot is saved for the end
            plan.append(rnd.choice([k for k in keys if k not in avoid]))
    return plan


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
    ad = bool(c.project.get("ad_mode"))
    take = c.one_take
    shots = take_plan(len(chunks)) if take else (ad_shot_plan if ad else shot_plan)(len(chunks), f"{c.id}-{title}-{vertical}")
    table = TAKE_SHOTS if take else AD_SHOTS
    shot_text = {k: t for k, t, *_ in table} if ad else dict(SHOTS)
    still_move = {k: m for k, _, _, m in table}
    move_of = TAKE_MOVE if take else AD_MOVE
    the_set = next((x["name"] for x in o.get("locations", []) if x.get("name")), "") if take else ""
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
                   "When the narration speaks about another place or time, the picture moves there.") if not ad else (
                "\n\nPRODUCT COMMERCIAL: every fragment comes with [camera: ...], a product shot; build the picture exactly "
                "as that shot and begin the prompt with it. The chosen product is the hero of EVERY picture, with its exact "
                "shape, colours, logo and labels; list it in items for every picture. When the narration names a benefit, "
                "the picture shows the very detail that proves it. Premium commercial lighting, rich contrast, clean "
                "backgrounds or stylish sets, dynamic elements that fit the product. Absolutely no people, faces, hands, "
                "fingers or body parts, and no text or letters except the product's own labels.")
            if take:
                fx += ("\n\nONE TAKE: every picture is the same set (" + (the_set or "the one location") + ") with the product "
                       "standing still in exactly the same spot, the same background, light and props; only the camera "
                       "position changes as the [camera: ...] says. No splashes, particles, levitation or new objects. Put "
                       "this set's name in location for every picture.")
            if any(ch.get("speaker") for ch in part):
                fx += ("\n\nFragments marked [Name says] are lines spoken by that character: show that character "
                       "speaking or reacting, close enough to read the face.")
            r = c.ask("05_scenes.txt", SCHEMA_PROMPTS, 32000, extra=fx + POSITIONS_RULE + STORY_LOGIC, title=title, characters=chars,
                      locations=locs, chunks=listing, orientation=orientation)
        by_index = {p.get("index"): p for p in r.get("prompts", [])}
        for i, ch in enumerate(part):
            p = by_index.get(start + i) or (r["prompts"][i] if i < len(r.get("prompts", [])) else None)
            visible = list((p or {}).get("characters") or [])
            if ch.get("speaker") in who and who[ch["speaker"]] not in visible:
                visible.append(who[ch["speaker"]])  # whoever speaks is on screen
            camera = shot_text[shots[start + i]]
            if ad:
                visible = []
            prompt = (p or {}).get("prompt") or f"A scene illustrating: {ch['text']}"
            if " ".join(camera.split()[:2]).lower().strip(",.:") not in prompt.lower() and not prompt.lower().startswith("camera"):
                prompt = f"Camera: {camera}. {prompt}"
            out.append({
                "text": ch["text"],
                "paragraph_end": ch["paragraph_end"],
                "prompt": prompt,
                "shot": shots[start + i],
                "prompt_ru": (p or {}).get("prompt_ru", ""),
                "motion": still_move[shots[start + i]] if ad else media.MOTIONS[(start + i) % len(media.MOTIONS)],
                "characters": visible,
                "items": ([c.char_name(it) for it in c.items] if ad else list((p or {}).get("items") or [])),
                **({"move": move_of[shots[start + i]]} if ad else {}),
                "location": the_set or (p or {}).get("location", ""),
                **({"speaker": ch["speaker"]} if ch.get("speaker") else {}),
                "effect": (p or {}).get("effect", "none"),
                "sfx": (p or {}).get("sfx", "none"),
            })
    return out


SCHEMA_LOGIC = {"type": "object", "properties": {"fixes": {"type": "array", "items": {"type": "object", "properties": {
    "index": {"type": "integer"}, "problem_ru": {"type": "string"}, "prompt": {"type": "string"},
    "characters": {"type": "array", "items": {"type": "string"}}}}}}}
SCHEMA_PIC_CHECK = {"type": "object", "properties": {
    "ok": {"type": "boolean"}, "problems_ru": {"type": "array", "items": {"type": "string"}},
    "fixed_prompt": {"type": "string"}}}


def auto_check_on(c: Ctx) -> bool:
    return bool(c.settings.get("auto_check", True)) and not c.demo


def _logic_check(c: Ctx, scenes: list):
    """One read of the whole story with all picture descriptions: contradictions are fixed before anything is drawn."""
    if not auto_check_on(c) or not scenes:
        return
    names = ", ".join(c.char_name(ch) for ch in c.characters) or "(none)"
    o = c.data("outline") or {}
    locs = "\n".join(f"- {x['name']}: {x['visual']}" for x in o.get("locations", [])) or "- (none)"
    listing = "\n\n".join(f"{i}. NARRATION: {s['text']}\n   LOCATION: {s.get('location', '')}\n   CHARACTERS: "
                           f"{', '.join(s.get('characters') or []) or '-'}\n   PICTURE: {s['prompt']}"
                           for i, s in enumerate(scenes))
    c.log("Контролёр логики проверяет описания всех картинок…")
    try:
        r = llm.call_json(_check_settings(c.settings), "You are the continuity supervisor of an animated video. Answer only with JSON.",
                          "Read the whole video: narration of each scene and the description of its picture. Find every "
                          "picture that contradicts the story or common sense: a person missing who must be there (a "
                          "moving taxi without its driver, a dialogue with one side gone), an extra or random person, a "
                          "person in the wrong place or seat, an empty seat that the story fills, wrong time of day, light "
                          "or weather for that moment, clothes or looks that change between scenes, an impossible camera "
                          "position, a metaphor mixed into a literal scene in a confusing way, readable text or letters.\n"
                          "For each such picture return its index, the problem in Russian (problem_ru) and the full "
                          "corrected description in English (prompt) that keeps the camera angle, place, light and every "
                          "correct detail and states positions explicitly; in characters list which recurring characters "
                          f"({names}) are visible in the corrected picture, even partly. Do not touch pictures that are fine; "
                          "if all are fine return an empty list.\n\nLOCATIONS:\n" + locs + "\n\nCHARACTERS:\n"
                          + "\n".join(f"- {c.char_name(ch)}: {ch.get('appearance', '')}" for ch in c.characters)
                          + "\n\nSCENES:\n" + listing, SCHEMA_LOGIC, c.usage, 32000)
        c.save_usage()
    except UserError as e:
        c.log(f"Контролёр логики не ответил ({e}), картинки рисуются по исходным описаниям.")
        return
    n = 0
    for f in r.get("fixes") or []:
        i = f.get("index")
        if isinstance(i, int) and 0 <= i < len(scenes) and (f.get("prompt") or "").strip():
            scenes[i]["prompt"] = f["prompt"].strip()
            if isinstance(f.get("characters"), list):
                scenes[i]["characters"] = f["characters"]
            scenes[i]["logic_fix_ru"] = f.get("problem_ru", "")
            n += 1
            c.log(f"Контролёр логики, сцена {i + 1}: {f.get('problem_ru', '')}")
    c.log(f"Контролёр логики: исправлено описаний {n}." if n else "Контролёр логики: противоречий не нашёл.")


def _check_settings(settings: dict) -> dict:
    """The checkers look at many pictures: the same provider, a light effort, and Sonnet instead of the
    expensive Opus or Fable (checking needs a sharp eye, not the best writer)."""
    s = dict(settings, anthropic_effort="low")
    if (s.get("anthropic_model") or "").startswith(("claude-opus", "claude-fable")):
        s["anthropic_model"] = "claude-sonnet-5-5"
    return s


def _picture_ok(c: Ctx, path: Path, scene: dict, vertical: bool) -> dict:
    from PIL import Image
    import io
    im = Image.open(path).convert("RGB")
    im.thumbnail((768, 768))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    chars = [ch for ch in c.characters if {ch.get("name", "").lower(), ch.get("name_en", "").lower()}
             & {n.lower() for n in scene.get("characters") or []}]
    who = "\n".join(f"- {c.char_name(ch)}: {ch.get('appearance', '')}" for ch in chars) or "- (no recurring characters)"
    r = _pic_check_call(c, "You check pictures for an animated video. Answer only with JSON.",
                      "Look at the picture as an ordinary viewer scrolling past it for 3 seconds. Fail it (ok=false) "
                      "ONLY for a serious problem that viewer would notice and find wrong: a missing or extra person, a "
                      "double of a character, a person in an impossible place (nobody at the wheel of a moving car, the "
                      "passenger driving, a giant face outside the window), a recurring character who is clearly a "
                      "different person, a deformed face, an extra arm or hand, a big caption or watermark across the "
                      "picture, or a picture of something else than the story moment. Everything else passes: natural "
                      "small text on objects (a TAXI sign, a number plate, a label, a screen), details that differ from "
                      "the description, camera distance, small props, framing and borders (the program crops them), style. If it fails, list the problems in Russian and write "
                      "fixed_prompt: the full description rewritten so the image model avoids these problems (keep "
                      "camera, place, light; state positions explicitly; no art style).\n\nNARRATION: "
                      f"{scene.get('text', '')}\nDESCRIPTION: {scene.get('prompt', '')}\nRECURRING CHARACTERS IN IT:\n{who}\n"
                      f"FORMAT: {'vertical 9:16' if vertical else 'horizontal 16:9'}",
                      SCHEMA_PIC_CHECK, c.usage, 3000, images=[buf.getvalue()])
    return r


_NO_HAIKU = set()  # Haiku turned out unavailable on this key: not tried again until restart


def _pic_check_call(c: Ctx, system: str, user: str, schema: dict, usage: dict, max_tokens: int, images=None):
    """The picture checker runs once per picture (hundreds on a long video): on Claude it uses the cheap Haiku,
    about 20 times cheaper than Sonnet; if Haiku is not available on the key, the usual checker model."""
    s = _check_settings(c.settings)
    if s.get("llm_provider", "anthropic") == "anthropic" and not _NO_HAIKU:
        try:
            return llm.call_json(dict(s, anthropic_model="claude-haiku-5-5"), system, user, schema, usage,
                                 max_tokens, images=images)
        except UserError as e:
            if "не найдена" not in str(e) and "нет доступа" not in str(e):
                raise
            _NO_HAIKU.add(1)
    return llm.call_json(s, system, user, schema, usage, max_tokens, images=images)


def _check_pictures(c: Ctx, items: list, refs: dict, places: dict, item_refs: dict):
    """items: (scene dict, picture path, vertical, index). A bad picture is redrawn once."""
    own = set(read_json(c.dir / "own_images.json", []) or [])
    todo = [x for x in items if x[1].exists()
            and f"{'short' if x[1].name.startswith('short') else 'scene'}:{x[3]}" not in own]
    if not todo:
        return
    c.log(f"Контролёр картинок проверяет {len(todo)} картинок…")
    stats = {"bad": 0, "fixed": 0}
    lock = threading.Lock()

    def work(x):
        c.check()
        scene, path, vertical, idx = x
        label = f"{'Шортс, сцена' if path.name.startswith('short') else 'Сцена'} {idx + 1}"
        for attempt in range(2):  # one redraw at most: a second rarely helps and doubles the cost
            try:
                r = _picture_ok(c, path, scene, vertical)
            except UserError as e:
                c.log(f"Контролёр картинок, {label.lower()}: не ответил ({e}).")
                return
            if r.get("ok", True):
                if attempt:
                    with lock:
                        stats["fixed"] += 1
                return
            problems = "; ".join(r.get("problems_ru") or []) or "есть ошибки"
            if attempt == 1:
                c.log(f"{label}: после перерисовки остались ошибки ({problems}). Можно перерисовать вручную.")
                return
            with lock:
                if attempt == 0:
                    stats["bad"] += 1
            c.log(f"{label}: {problems}. Перерисовываю…")
            if (r.get("fixed_prompt") or "").strip():
                scene["prompt"] = r["fixed_prompt"].strip()
            backup = path.with_suffix(".bad.png")
            path.replace(backup)
            try:
                _make_image(c, scene["prompt"], path, vertical, idx, refs=refs.get(path), place=places.get(path),
                            items=item_refs.get(path))
            except UserError as e:
                backup.replace(path)
                c.log(f"{label}: перерисовать не вышло ({e}), оставлена прежняя картинка.")
                return
            backup.unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=max(1, int(c.settings.get("image_parallel") or 4))) as ex:
        for f in as_completed([ex.submit(work, x) for x in todo]):
            f.result()
            c.save_usage()
    c.log(f"Контролёр картинок: с ошибками было {stats['bad']}, исправлено {stats['fixed']}."
          if stats["bad"] else "Контролёр картинок: все картинки в порядке.")


SCHEMA_WORLD = {"type": "object", "properties": {
    "locations": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "visual": {"type": "string"}}}},
    "characters": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "visual": {"type": "string"}}}}}}


def _montage_world(c: Ctx, script: str):
    """Montage has no writer's outline: the places and people of the owner's text are described once, so every
    picture of one place shows the same place and every person keeps one look."""
    o = c.data("outline") or {}
    if o.get("locations") or c.demo:
        return
    c.log("Описываю места и людей из вашего текста, чтобы на всех картинках они были одинаковыми…")
    known = ", ".join(c.char_name(ch) for ch in c.characters)
    try:
        r = llm.call_json(_check_settings(c.settings), "You prepare the visual world of an illustrated video. Answer only with JSON.",
                          "Read the narration below. List the places where its scenes happen (locations: a short name and "
                          "visual, one English sentence with the look, light and key objects of the place) and the people "
                          "who appear in it (characters: name as in the text or a short role like 'the passenger', and "
                          "visual, one English sentence with age, build, hair and clothes). Merge places that are the same "
                          "place. At most 8 locations and 8 characters."
                          + (f" These recurring characters already have a fixed look, do not list them: {known}." if known else "")
                          + "\n\nNARRATION:\n" + script[:20000], SCHEMA_WORLD, c.usage, 4000)
        c.save_usage()
    except UserError as e:
        c.log(f"Описать места не получилось ({e}), картинки рисуются без образцов мест.")
        return
    o["locations"] = [x for x in r.get("locations") or [] if (x.get("name") or "").strip() and x.get("visual")][:8]
    o["characters"] = [x for x in r.get("characters") or [] if (x.get("name") or "").strip() and x.get("visual")][:8]
    write_json(c.out("outline"), o)


def st_scenes(c: Ctx):
    if c.opts.get("translate_from"):
        return _translate_scenes(c)
    script = c.data("review")["final_script"]
    if c.opts.get("montage"):
        _montage_world(c, script)
    chunks = split_chunks(c.strip_host_labels(script), c.scene_words("short_words_per_scene" if c.short_format else "words_per_scene"),
                          c.speakers())
    if c.one_take:
        chunks = take_chunks(chunks, c.take_shots())
    c.log(f"Сценарий разбит на {len(chunks)} сцен, пишу описания картинок…")
    scenes = _prompts_for(c, chunks, c.vertical, (c.data("outline") or {}).get("title", ""))
    _logic_check(c, scenes)
    reused = _reuse_pictures(c, scenes)
    if reused:
        c.log(f"Текст {len(reused)} сцен из {len(scenes)} не изменился: их картинки и анимации взяты из прошлой "
              "версии, заново нарисуются только новые и изменённые.")
    _name_speakers(c, scenes)
    write_json(c.out("scenes"), {"scenes": scenes})


def _name_speakers(c: Ctx, scenes: list):
    names = {ch["id"]: c.char_name(ch) for ch in c.characters}
    for sc in scenes:
        if sc.get("speaker") in names:
            sc["speaker_name"] = names[sc["speaker"]]


def merge_tags(tags: list, owner: str) -> list:
    """The video's own search phrases first, then the owner's permanent ones, then the rest; no duplicates."""
    own = [t.strip().lstrip("#") for t in re.split(r"[,;\n]", owner or "") if t.strip().lstrip("#")]
    out, seen = [], set()
    for t in tags[:6] + own + tags[6:]:
        t = " ".join(str(t).lstrip("#").split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


THUMB_RULE = (
    "\n\nTHUMBNAIL IS A MUST-CLICK (the owner's strict rule): thumbnail_prompt and thumbnail_text make a stop-scroll, "
    "clickbait cover on the edge: one huge face with an extreme emotion (shock, terror, fury, disbelief) or a shocking, "
    "absurd or forbidden-looking moment from the script, a strong curiosity gap, maximum contrast and saturated "
    "colours, readable at phone size in a split second. Provocative and intriguing, but no nudity, gore or real "
    "celebrities, and nothing that is not in the video.")


def st_extras(c: Ctx):
    script = c.data("review")["final_script"]
    if c.demo:
        r = _demo("extras")
    else:
        c.log("Готовлю название, описание и теги" + ("…" if c.lang == "ru" else ", перевожу сценарий на русский…"))
        seo = (c.project.get("seo_ru") or "").strip()
        r = c.ask("06_extras.txt", SCHEMA_EXTRAS, 32000, script=script, extra=THUMB_RULE + (
            "\n\nTHE OWNER'S PERMANENT SEARCH PHRASES FOR THIS CHANNEL (use the ones that fit in the description and "
            f"hashtags; all of them go into the tags):\n{seo}" if seo else ""))
        r["tags"] = merge_tags(r.get("tags") or [], seo)
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
    if c.opts.get("translate_from"):
        return _translate_short(c)
    script = c.data("review")["final_script"]
    if c.demo:
        r = _demo("short")
    else:
        c.log("Пишу сценарий шортса…")
        r = c.ask("07_short.txt", SCHEMA_SHORT, 16000, script=script)
    if c.lang == "ru" and not r.get("script_ru"):
        r["script_ru"] = r["script"]
    r["checks"] = script_checks(r["script"], int(125 * c.wpm / WORDS_PER_MIN), c.wpm)
    r["script"] = c.strip_host_labels(r["script"])
    chunks = split_chunks(r["script"], c.scene_words("short_words_per_scene"), c.speakers())
    r["scenes"] = _prompts_for(c, chunks, True, r.get("title", ""))
    _logic_check(c, r["scenes"])
    _name_speakers(c, r["scenes"])
    write_json(c.out("short_script"), r)


def st_approval(c: Ctx):
    if c.opts.get("review_pause") and not db.get(c.id).get("approved"):
        raise StopForReview()
    write_json(c.out("approval"), {"approved_at": time.time()})


# -------- voice

def mute_seconds(text: str) -> float:
    """How long a line of on-screen text stays when there is no voice: enough to read it calmly."""
    return round(min(6.0, max(2.2, 1.0 + word_count(text) * 0.42)), 2)


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
        if c.mute:  # silence as long as the line takes to read, or one whole clip in one take
            media.silence(wav, TAKE_CLIP if c.one_take else mute_seconds(sc["text"]))
            return i
        if c.demo:
            media.demo_audio(mp3, max(1.5, word_count(sc["text"]) / 2.5), i)
        elif mp3.exists() and tts.is_audio(mp3.read_bytes()[:16]):
            pass  # only the speech tempo changed: the voice already said this, no new ElevenLabs characters
        else:
            who = sc.get("speaker", "")
            same = lambda s: s.get("speaker", "") == who  # context only from the same voice
            prev_t = " ".join(s["text"] for s in scenes[max(0, i - 3):i] if same(s))
            next_t = scenes[i + 1]["text"] if i + 1 < len(scenes) and same(scenes[i + 1]) else ""
            settings = c.settings
            ch = next((x for x in c.characters if x["id"] == who and x.get("voice_id")), None)
            if ch:
                settings = {**c.settings, "elevenlabs_voice_id": ch["voice_id"]}
            chars = tts.synthesize(settings, sc["text"], mp3, prev_t, next_t, c.lang)
            c.add_usage("tts_chars", chars)
        media.to_wav(mp3, wav, c.voice_tempo())
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
    c.log(f"Без озвучки: {len(scenes)} сцен под музыку, без текста на экране…" if c.mute else
          f"Озвучиваю {len(scenes)} фрагментов голосом {c.settings.get('elevenlabs_voice_name') or ''}…")
    for n, _ in _voice_list(c, scenes, "scene"):
        c.progress(n / total)
    gap, pgap = float(c.settings["scene_gap"]), float(c.settings["paragraph_gap"])
    if c.short_format:
        gap, pgap = 0.1, 0.2  # Shorts need a fast, continuous voice
    if c.mute:
        gap, pgap = 0.0, 0.0  # the silence of each line is already its reading time
    gap, pgap = gap / c.voice_tempo(), pgap / c.voice_tempo()  # a faster manner has shorter pauses too
    # A long sentence cut at a comma is one breath: no scene pause inside it.
    mid = lambda t: not re.search(r"[.!?…][\"”’')\]»]*\s*$", t)
    parts = [(c.dir / "audio" / f"scene_{i:03d}.wav",
              pgap if sc["paragraph_end"] else min(gap, 0.04) if mid(sc["text"]) else gap)
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
    words = sum(word_count(sc.get("text", "")) for sc in scenes)
    if not c.demo and not c.mute and words >= 150 and result["total"] > 30:
        record_wpm(c.pace_key(), words / (result["total"] / 60))


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


def _named_in(name: str, text: str) -> bool:
    """A whole-word match, so "Max" is not found inside "maximum"."""
    return bool(re.search(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)", text.lower()))


def _thumb_items(c: Ctx, prompt: str) -> list:
    named = [n for it in c.items for n in (it.get("name", ""), it.get("name_en", "")) if n and _named_in(n, prompt)]
    return _item_refs_for(c, {"items": named})


def _thumb_refs(c: Ctx, prompt: str) -> list:
    """The thumbnail shows the channel's own hero: the characters named in its prompt, else the host or the first one."""
    names = [n for ch in c.characters for n in (ch.get("name", ""), ch.get("name_en", "")) if n]
    named = [n for n in names if _named_in(n, prompt)]
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
    keep = bool(c.project.get("keep_places") and c.project.get("id"))
    if keep:
        from . import projects
    for (key, vertical), loc in need.items():
        out = c.dir / "images" / f"loc_{names.index(key):02d}{'_v' if vertical else ''}.png"
        saved = projects.place_picture(c.project["id"], loc["name"], vertical) if keep and not out.exists() else None
        if saved:
            shutil.copy(saved, out)
            c.log(f"Место «{loc['name']}» беру из сохранённых мест проекта, бесплатно.")
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
            if keep:
                try:
                    projects.save_place(c.project["id"], loc["name"], loc.get("visual", ""), out, vertical)
                    c.log(f"Место «{loc['name']}» сохранено в проекте для следующих серий.")
                except OSError as e:
                    c.log(f"Место «{loc['name']}» сохранить в проекте не получилось: {e}")
        plates[(key, vertical)] = out
    return plates


# Image models sometimes draw a vertical picture as a smaller one on white paper; this asks for the whole frame.
FULL_BLEED = "Full-bleed scene filling the entire frame edge to edge"


def _wrong_shape(path: Path, vertical: bool = True) -> bool:
    from PIL import Image
    with Image.open(path) as im:
        return im.width > im.height * 0.8 if vertical else im.height > im.width * 0.8


def _make_image(c: Ctx, prompt: str, out: Path, vertical: bool, idx: int, fallback: Path = None, refs: list = None,
                place: Path = None, items: list = None, example: bool = False):
    if out.exists():
        return
    if c.demo:
        media.demo_image(out, idx, vertical)
        return
    style = (c.settings.get("image_style") or "").strip().rstrip(".")
    # The style goes first: image models read only the start of a long prompt, and the end is often cut off.
    full = (f"{style}. " if style else "") + f"{prompt.strip().rstrip('.')}. {FULL_BLEED}"
    if refs or place or items:
        try:
            price = images.reference_price(c.settings, refs, place, items)
            raw = images.generate_with_reference(c.settings, full, refs or [], vertical, place=place, items=items,
                                                 example=example)
            media.to_png(raw, out)
            media.trim_borders(out)
            if _wrong_shape(out, vertical):
                # The editing model sometimes keeps the shape of a reference photo: one more try, then the
                # picture is cropped to fill the frame.
                c.log(f"Картинка {idx + 1}: получилась не той формы, рисую ещё раз…")
                c.add_usage("image_cost", price)
                balance.record("fal", price)
                raw = images.generate_with_reference(c.settings, ("Tall vertical 9:16 portrait composition. " if vertical
                                                                  else "Wide horizontal 16:9 landscape composition. ") + full,
                                                     refs or [], vertical, place=place, items=items, example=example)
                media.to_png(raw, out)
                media.trim_borders(out)
            c.add_usage("images", 1)
            c.add_usage("image_cost", price)
            balance.record("fal", price)
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


def make_package(jid: str, text: str = None, side: str = None, font: str = None, look: str = None):
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
    if font is not None:
        opts["font"] = font
    if look is not None:
        opts["look"] = look
    write_json(d / "thumb.json", opts)
    (d / "output").mkdir(exist_ok=True)
    src = d / "images" / "thumbnail_src.png"
    job = db.get(jid) or {}
    vertical = (job.get("options") or {}).get("format") == "short"
    if src.exists():
        media.thumbnail(src, d / "output" / "thumbnail_clean.png", vertical)
        thumb.render(src, d / "output" / "thumbnail.jpg", opts.get("text", extras.get("thumbnail_text", "")),
                     opts.get("side", "left"), vertical, opts.get("font") or extras.get("thumbnail_font", ""),
                     opts.get("look") or extras.get("thumbnail_look", ""))
        (d / "output" / "thumbnail.png").unlink(missing_ok=True)  # older versions
    short = read_json(d / "short_script.json")
    thumb.write_package(d, extras, short)
    if (job.get("stage") or "") == "done":
        export(jid)


def needs_images_choice(job: dict) -> bool:
    """The owner asked to stop before pictures to upload his own, and has not pressed «Continue» yet."""
    opts = job.get("options", {})
    return (job.get("status") == "review" and bool(opts.get("images_pause")) and not opts.get("images_picked")
            and bool(job.get("id")) and (job_dir(job["id"]) / "voice.json").exists())


SCHEMA_TAKE = {"type": "object", "properties": {
    "product": {"type": "array", "items": {"type": "number"}},
    "detail": {"type": "array", "items": {"type": "number"}},
    "detail_ru": {"type": "string"},
    "detail2": {"type": "array", "items": {"type": "number"}},
    "detail2_ru": {"type": "string"}},
    "required": ["product", "detail", "detail_ru", "detail2", "detail2_ru"]}


def _take_boxes(c: Ctx, master: Path) -> dict:
    """Where the product and its two most selling details are in the picture (fractions x0, y0, x1, y1)."""
    from PIL import Image
    import io
    im = Image.open(master).convert("RGB")
    im.thumbnail((1024, 1024))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    names = ", ".join(f"{x.get('name', '')} ({x.get('appearance', '')[:300]})" for x in c.items) or "the product"
    try:
        return llm.call_json(_check_settings(c.settings), "You mark regions of a product photo. Answer only with JSON.",
                             "A camera will glide over this product photo for an ad: push in, macro of details, pull back. "
                             "Give boxes as fractions of the picture width and height: [x0, y0, x1, y1], 0..1, "
                             "(0, 0) is the top-left corner. product: a tight box around the whole product. detail: "
                             "a tight box around its key functional part (the blade, the nozzle, the lens, the cap…). "
                             "detail2: a tight box around a second attractive detail of the product (a button, a logo, "
                             "a texture, a metal accent), different from detail. Both details lie inside the product "
                             f"box. Name each detail in Russian in 2-4 words.\nPRODUCT: {names}",
                             SCHEMA_TAKE, c.usage, 2000, images=[buf.getvalue()])
    except UserError as e:
        c.log(f"Не удалось разметить товар на кадре ({e}), камера пойдёт по центру.")
        return {"product": [0.3, 0.25, 0.7, 0.8], "detail": [], "detail2": [], "detail_ru": "", "detail2_ru": ""}


def _take_orbits(c: Ctx, scenes: list, result: dict):
    """One take: chosen scenes become a Kling orbit around the product, starting from that scene's framing.
    The rest stays a camera move over the one picture. A failed orbit simply falls back to that move."""
    from PIL import Image
    master = c.dir / "images" / "scene_000.png"
    tk = read_json(c.dir / "take.json")
    if not tk or len(tk.get("path") or []) != len(scenes):
        _take_frames(c, scenes, master)
        tk = read_json(c.dir / "take.json")
    mode = c.project.get("take_orbit") or "one"
    sel = take.orbit_scenes(len(scenes), mode)
    result.update(scene=[], short=[], one_take=True, orbits=[])
    if not sel:
        c.log("Одним дублем без облёта: камера поедет по одному кадру, товар не исказится, Kling не нужен.")
        write_json(c.out("animate"), result)
        return
    aset = {**c.settings, "video_model": "kling25"}
    adir = c.dir / "anim"
    adir.mkdir(exist_ok=True)
    w, h = (1080, 1920) if c.vertical else (1920, 1080)
    big_path = c.dir / "images" / "take_big.jpg"
    if not big_path.exists():
        take.prepare(master, big_path)
    big = Image.open(big_path).convert("RGB")
    fr = take.framings(big.width, big.height, w / h, tk["product"], tk.get("detail") or None, tk.get("detail2") or None)
    todo = [i for i in sel if not (adir / f"scene_{i:03d}.mp4").exists()]
    est = sum(animate.cost(aset, TAKE_CLIP) for _ in todo)
    c.log(f"Одним дублем: облёт товара в {len(sel)} сц. через Kling 2.5 Turbo Pro (новых {len(todo)}, примерно "
          f"${est:.2f}). Остальное: камера по кадру, без искажений.")
    done = [len(sel) - len(todo)]

    def work(i):
        c.check()
        a = tk["path"][i][0]
        start = c.dir / "images" / f"orbit_{i:03d}.png"
        # the orbit starts on the product seen whole: a macro start would leave Kling nothing to keep rigid
        take.crop(big, fr["push" if a.rstrip("~") in ("wide", "detail", "detail2", "base") and mode != "all" else a],
                  w, h).save(start)
        out = adir / f"scene_{i:03d}.mp4"
        try:
            data = animate.generate(aset, start, scenes[i].get("prompt") or "", TAKE_CLIP, c.vertical, c.check,
                                    motion=animate.orbit_motion("left" if i % 2 == 0 else "right"),
                                    negative=animate.ORBIT_NEGATIVE)
            tmp = out.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(out)
            c.add_usage("anim_cost", animate.cost(aset, TAKE_CLIP))
            balance.record("fal", animate.cost(aset, TAKE_CLIP))
        except UserError as e:
            if "ключ" in str(e) or "денег" in str(e) or "лимит" in str(e):
                raise
            result["failed"].append(f"scene_{i:03d}")
            c.log(f"Облёт в сцене {i + 1} не получился ({e}). Там будет камера по кадру.")

    with ThreadPoolExecutor(max_workers=3) as ex:
        for f in as_completed([ex.submit(work, i) for i in todo]):
            f.result()
            done[0] += 1
            c.progress(done[0] / len(sel))
            c.save_usage()
    result["orbits"] = [i for i in sel if (adir / f"scene_{i:03d}.mp4").exists()]
    sc_data = c.data("scenes")
    for i in result["orbits"]:
        sc_data["scenes"][i]["move"] = "облёт вокруг товара (Kling)"
    write_json(c.out("scenes"), sc_data)
    write_json(c.out("animate"), result)


def _take_frames(c: Ctx, scenes: list, master: Path):
    """Plans the camera path over the one picture and saves a still of each scene's opening framing."""
    from PIL import Image
    tk_file = c.dir / "take.json"
    tk = read_json(tk_file)
    if not tk:
        c.log("Одним дублем: размечаю товар и его детали на кадре для наездов и макро…")
        tk = _take_boxes(c, master)
    tk["path"] = [list(x) for x in take.path_for(len(scenes))]
    write_json(tk_file, tk)
    im = Image.open(master).convert("RGB")
    w, h = (1080, 1920) if c.vertical else (1920, 1080)
    fr = take.framings(im.width, im.height, w / h, tk["product"], tk.get("detail") or None, tk.get("detail2") or None)
    data = c.data("scenes")
    names = dict(take.NAMES)
    if tk.get("detail_ru"):
        names["detail"] = f"макро: {tk['detail_ru']}"
    if tk.get("detail2_ru"):
        names["detail2"] = f"макро: {tk['detail2_ru']}"
    for i, (a, b) in enumerate(tk["path"]):
        sc = data["scenes"][i]
        sc["move"] = f"{names[a.rstrip('~')]} → {names[b.rstrip('~')]}" if a.rstrip("~") != b.rstrip("~") \
            else f"медленно: {names[b]}"
        sc["one_take"] = True
        if i:
            cx, cy, hh = fr[a]
            ww = hh * w / h
            im.crop((round(cx - ww / 2), round(cy - hh / 2), round(cx + ww / 2), round(cy + hh / 2))) \
                .resize((w, h), Image.LANCZOS).save(c.dir / "images" / f"scene_{i:03d}.png")
    scenes[:] = data["scenes"]
    write_json(c.out("scenes"), data)
    (c.dir / "images" / "take_big.jpg").unlink(missing_ok=True)


def st_images(c: Ctx):
    job = db.get(c.id) or {"options": c.opts}
    if job["options"].get("images_pause") and not job["options"].get("images_picked"):
        raise StopForReview("⏸ Сценарий, сцены и озвучка готовы. Загрузите свои картинки в шаге «Сцены и описания "
                            "картинок» и нажмите «Продолжить»: недостающие программа дорисует сама.")
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
    places, examples = {}, set()
    if _same_place(c):
        kinds = [(scenes, c.vertical)] + ([(short["scenes"], True)] if short else [])
        plates = _location_plates(c, kinds)
        for scs, vertical, prefix in [(scenes, c.vertical, "scene")] + ([(short["scenes"], True, "short")] if short else []):
            for i, sc in enumerate(scs):
                pl = plates.get(((sc.get("location") or "").strip().lower(), vertical))
                out = img / f"{prefix}_{i:03d}.png"
                if pl and c.settings.get("ref_model") == "flux2" and sc.get("shot") in ("closeup", "detail") \
                        and not refs.get(out) and not item_refs.get(out):
                    continue  # a close-up or a detail shows almost no background: the cheap model draws it
                if pl:
                    places[out] = pl
    if c.one_take and scenes and not c.demo:
        # One take: ONE picture of the product; every scene is a camera move over it, so the product never changes.
        master = img / "scene_000.png"
        if not master.exists():
            c.log("Одним дублем: рисую один кадр с товаром, камера потом будет ездить по нему…")
            _make_image(c, scenes[0]["prompt"], master, c.vertical, 0, refs=refs.get(master), place=places.get(master),
                        items=item_refs.get(master))
        _take_frames(c, scenes, master)
    extras = c.data("extras") or {}
    if extras.get("thumbnail_prompt"):
        jobs.append((extras["thumbnail_prompt"], img / "thumbnail_src.png", c.vertical, 999))
        refs[img / "thumbnail_src.png"] = _thumb_refs(c, extras["thumbnail_prompt"])
        item_refs[img / "thumbnail_src.png"] = _thumb_items(c, extras["thumbnail_prompt"])
        example = img / Path(extras.get("thumbnail_ref") or "_").name
        if extras.get("thumbnail_ref") and example.is_file():  # the owner picked a picture of this video as the example
            places[img / "thumbnail_src.png"] = example
            examples.add(img / "thumbnail_src.png")
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
                        items=item_refs.get(out), example=out in examples)
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
            first = next((p for p in sorted(img.glob("scene_*.png"))), None)
            if first:
                shutil.copy(first, out)
                c.log("Картинку для превью нарисовать не получилось, поставил картинку первой сцены. В шаге «Перевод и "
                      "описание для YouTube» можно перерисовать превью с исправленным описанием или загрузить своё.")
            continue
        if not src:
            raise UserError(f"Не удалось сделать картинку {idx + 1}. Попробуйте шаг ещё раз или смените сервис.")
        shutil.copy(src, out)
        c.log(f"Картинка {idx + 1}: подставлена соседняя. Можно перегенерировать её вручную.")
    if auto_check_on(c):
        checks = [(sc, img / f"scene_{i:03d}.png", c.vertical, i) for i, sc in enumerate(scenes)
                  if (img / f"scene_{i:03d}.png") in {j[1] for j in todo}]
        if short:
            checks += [(sc, img / f"short_{i:03d}.png", True, i) for i, sc in enumerate(short["scenes"])
                       if (img / f"short_{i:03d}.png") in {j[1] for j in todo}]
        failed_paths = {j[1] for j in failed}
        _check_pictures(c, [x for x in checks if x[1] not in failed_paths], refs, places, item_refs)
        write_json(c.out("scenes"), {"scenes": scenes})  # the checker may have improved descriptions
        if short:
            write_json(c.out("short_script"), short)
    make_package(c.id)
    c.save_usage()
    write_json(c.out("images"), {"count": len(jobs), "replaced": len(failed)})


# -------- video

def subs_on(job_or_opts, kind: str, settings: dict = None) -> bool:
    """Whether subtitles are burned into the long video ("video") or the short ("short")."""
    opts = job_or_opts.get("options", job_or_opts)
    if opts.get("voice_off"):
        return False  # no voice: the script is only the plan of the shots, nothing is written on screen
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
    # Only once the pictures are ready: earlier stops (topics, script, own pictures) are not about animation.
    return (job.get("status") == "review" and bool(opts.get("anim_pause")) and not opts.get("anim_picked")
            and bool(job.get("id")) and (job_dir(job["id"]) / "images.json").exists())


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
    if c.one_take and not c.demo:
        _take_orbits(c, scenes, result)
        return
    if not sel and not ssel:
        c.log("Анимация выключена: в видео будут картинки с плавным движением камеры.")
        write_json(c.out("animate"), result)
        return
    adir = c.dir / "anim"
    adir.mkdir(exist_ok=True)
    work_items = [("scene", i, scenes[i], voice["durations"][i], c.vertical) for i in sel]
    work_items += [("short", i, short["scenes"][i], voice["short_durations"][i], True) for i in ssel]
    todo = [w for w in work_items if not (adir / f"{w[0]}_{w[1]:03d}.mp4").exists()]
    # One take: every clip flies from its picture to the next one (Kling's end frame), so the cuts disappear.
    aset = {**c.settings, "video_model": "kling25"} if c.one_take else c.settings
    if c.one_take and c.settings.get("video_model") not in (None, "", "kling25"):
        c.log("Одним дублем: для плавных переходов кадр в кадр беру Kling 2.5 Turbo Pro (он умеет конечный кадр).")
    est = sum(animate.cost(aset, w[3]) for w in todo) if not c.demo else 0
    c.log(f"Оживляю сцены: {len(todo)} из {len(work_items)} "
          f"({animate.model(aset)['name'].split(' — ')[0]}, примерно ${est:.2f})…")
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
                nxt = c.dir / "images" / f"{prefix}_{i + 1:03d}.png"
                tail = nxt if c.one_take and nxt.exists() else None
                motion = (animate.take_motion(sc.get("move", ""), bool(tail)) if c.one_take
                          else animate.ad_motion(sc.get("move", "")) if c.project.get("ad_mode") else None)
                data = animate.generate(aset, img, sc["prompt"], need, vertical, c.check, motion=motion, tail=tail)
                tmp = out.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(out)
                c.add_usage("anim_cost", animate.cost(aset, need))
                balance.record("fal", animate.cost(aset, need))
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
    anim |= {int(k.split(":")[1]) for k in read_json(c.dir / "own_clips.json", []) or [] if k.startswith(prefix + ":")}
    fx = effects.plan(scenes, fx_mode(db.get(c.id) or c.opts), height > width)
    if c.one_take:  # one smooth camera move: no punches or shakes between the shots
        fx = [(i, "none", sfx if sfx == "whoosh" else "none") for i, _e, sfx in fx]
    tk = None
    if c.one_take and prefix == "scene" and not c.demo and (c.dir / "images" / "scene_000.png").exists():
        tk = read_json(c.dir / "take.json")
        if not tk or len(tk.get("path") or []) != len(scenes):  # pictures made before: the first one is the frame
            _take_frames(c, scenes, c.dir / "images" / "scene_000.png")
            tk = read_json(c.dir / "take.json")
    if tk and len(tk.get("path") or []) == len(scenes):
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        big_path = c.dir / "images" / "take_big.jpg"
        if not big_path.exists():
            take.prepare(c.dir / "images" / "scene_000.png", big_path)
        big = Image.open(big_path).convert("RGB")
        fr = take.framings(big.width, big.height, width / height, tk["product"], tk.get("detail"), tk.get("detail2"))
        path = tk["path"]
        orbits = set((read_json(c.out("animate"), {}) or {}).get("orbits", []))
        fx = [(i, "none", "whoosh" if i and path[i][0] != path[i - 1][1] else "none") for i in range(len(scenes))]
        c.log("Одним дублем: камера едет по кадру с товаром (наезды, макро, отъезд), товар не перерисовывается.")
    else:
        tk = None

    def work(i):
        c.check()
        a, b = bounds[i]
        orbit = c.dir / "anim" / f"{prefix}_{i:03d}.mp4"
        if tk and i in orbits and orbit.exists():
            media.render_anim_clip(orbit, clips[i], b - a, width, height, fps,
                                   fade_in=(i == 0), fade_out=(i == len(scenes) - 1), effect="none")
            return
        if tk:
            take.render_scene(big, fr[path[i][0]], fr[path[i][1]], b - a, clips[i], width, height, fps,
                              fade_in=(i == 0), fade_out=(i == len(scenes) - 1), glint=(i == len(scenes) - 1))
            return
        src = c.dir / "anim" / f"{prefix}_{i:03d}.mp4"
        if i in anim and src.exists():
            media.render_anim_clip(src, clips[i], b - a, width, height, fps,
                                   fade_in=(i == 0), fade_out=(i == len(scenes) - 1), effect=fx[i][1],
                                   fit=c.one_take and i < len(scenes) - 1)
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
    blocked = {}  # caption places taken at some moments by an animation or the subscribe buttons
    opts = (db.get(c.id) or {}).get("options") or c.opts
    if opts.get("graphics"):
        shown = _graphics_for(c, prefix, scenes, durations, total, height > width)
        if shown:
            with_g = clips_dir / "joined_gfx.mp4"
            try:
                n = graphics.overlay(joined, with_g, shown, width, height, clips_dir)
                with_g.replace(joined)
                c.log(f"Анимации данных: {n} ({', '.join(graphics.TEMPLATES[g['type']][0].lower() for g in shown)}).")
                if height <= width:  # on a wide video the animations sit at the top, where captions could go
                    blocked["top"] = [(g["start"], g["start"] + graphics.DUR) for g in shown]
            except Exception as e:  # noqa: BLE001
                c.log(f"Анимации данных добавить не получилось: {e}")
    if height > width:
        src = c.project if c.project.get("id") else c.settings
        mode = c.opts.get("cta_short") or src.get("cta_short") or "both"
        lang_pick = c.opts.get("cta_lang") or src.get("cta_lang")
        lang = lang_pick if lang_pick in cta.TEXT else c.lang
        blocked.setdefault("upper", []).extend((st, st + cta.DURATION[k] + 0.3) for k, st in cta.plan(mode, total))
        with_cta = clips_dir / "joined_cta.mp4"
        try:
            if cta.overlay(joined, with_cta, mode, lang, total, width, height, clips_dir):
                with_cta.replace(joined)
                c.log("Добавил анимацию «Подписаться» и «Нравится» во второй половине шортса.")
        except UserError as e:
            c.log(f"Анимацию «Подписаться» добавить не получилось: {e}")
    music = _music_bed(c, prefix, scenes, durations, total, clips_dir, width, height) or _music_for(c, total)
    out.parent.mkdir(exist_ok=True)
    smart = opts.get("subs_smart", False)
    cap = captions.build(c.dir, prefix, scenes, durations, width, height, smart=smart, blocked=blocked)
    if subs and cap.get("moved"):
        c.log(f"Субтитры: {cap['moved']} строк перенесено в свободное место кадра.")
    sfx_file = clips_dir / "sfx.wav"
    has_sfx = effects.build_track(fx, durations, sfx_file, float(c.settings.get("sfx_volume", 0.5)))
    if has_sfx:
        c.log(f"Звуковые эффекты: {sum(1 for f in fx if f[2] != 'none')}")
    if subs:
        c.log("Накладываю субтитры на видео…")
    vol = opts.get("music_volume")
    if c.mute:
        vol = 1.0  # the music is the whole sound
        if not music:
            c.log("⚠️ Ролик без озвучки, а музыки нет: он будет беззвучным. Добавьте треки в «Музыку» или включите «Сочинять новую».")
    media.final_mix(joined, narration, music, out, float(vol if vol not in (None, "") else c.settings["music_volume"]),
                    float(c.settings["loudness_lufs"]), total,
                    subs=cap["ass"] if subs else None, fonts=cap["fonts"], sfx=sfx_file if has_sfx else None)
    joined.unlink(missing_ok=True)
    sfx_file.unlink(missing_ok=True)
    return {"file": str(out.relative_to(c.dir)), "duration": total, "music": music.name if music else "",
            "subtitles": subs}


def _graphics_for(c: Ctx, prefix: str, scenes: list, durations: list, total: float, vertical: bool) -> list:
    """Data animations of this video with their start times. The plan (which scene, which template, what text)
    is asked once and kept, so a new assembly costs nothing."""
    path = c.dir / f"graphics_{prefix}.json"
    plan = read_json(path)
    if plan is None:
        if c.demo:
            plan = {"items": []}
        else:
            c.log("Ищу в тексте цифры и важные моменты для анимаций…")
            try:
                plan = llm.call_json(_check_settings(c.settings), "You plan on-screen data animations. Answer only with JSON.",
                                     graphics.plan_prompt(scenes, vertical, c.lang), graphics.SCHEMA, c.usage, 6000)
                c.save_usage()
            except Exception as e:  # noqa: BLE001 - animations are an extra, never a reason to lose the video
                c.log(f"Подобрать анимации не получилось ({e}), ролик собирается без них.")
                return []
        write_json(path, plan)
    try:
        words = captions.timeline(c.dir, prefix, scenes, durations)
        return graphics.place(plan.get("items") or [], words, durations, total, vertical)
    except Exception as e:  # noqa: BLE001
        c.log(f"Расставить анимации не получилось ({e}), ролик собирается без них.")
        return []


SCHEMA_MUSIC = {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}


def _music_prompt(c: Ctx, wish: str, extras: dict) -> str:
    """The owner's music wish (often in Russian) as an English prompt for the music model. The owner's style wins;
    the video's mood only fills in what he did not say."""
    try:
        r = llm.call_json(c.settings, "You write prompts for an AI music generator. Answer only with JSON.",
                          f"The owner wants this background music (may be in Russian): {wish}\n"
                          f"The video's mood, only for what the owner did not specify: {extras.get('music_prompt', '')}\n"
                          "Return prompt: one English prompt for instrumental background music under a voiceover: genre, "
                          "mood, tempo, instruments, exactly as the owner asked. No vocals, nothing that distracts.",
                          SCHEMA_MUSIC, c.usage, 2000)
        c.save_usage()
        return (r.get("prompt") or "").strip() or wish
    except UserError:
        return f"Instrumental background music, no vocals. Style: {wish}"


SCHEMA_MUSIC = {"type": "object", "properties": {
    "parts": {"type": "array", "items": {"type": "object", "properties": {
        "from_scene": {"type": "integer"}, "mood_ru": {"type": "string"}, "energy": {"type": "integer"},
        "track": {"type": "string"}}, "required": ["from_scene", "mood_ru", "energy", "track"]}},
    "breaths": {"type": "array", "items": {"type": "integer"}}},
    "required": ["parts", "breaths"]}
XFADE = 2.0   # seconds of crossfade between two tracks
BREATH = 0.8  # seconds the music takes to fade out before a dramatic beat and back in after it


def _music_bed(c: Ctx, prefix: str, scenes: list, durations: list, total: float, clips_dir: Path,
               width: int, height: int):
    """Long videos: a music bed that follows the script. A music editor splits the video into parts by mood and
    picks a library track of the right energy for each; the parts crossfade, and before a few big turns the music
    holds its breath. Returns a wav of the video's length, or None to use one track as before."""
    opts = (db.get(c.id) or {}).get("options") or c.opts
    if (c.demo or height > width or c.project.get("music_smart") is False or opts.get("music_track")
            or (opts.get("music_mode") or "library") != "library" or len(scenes) < 6 or total < 90):
        return None
    files = musiclib.tracks()
    if len(files) < 2:
        return None
    plan_file = c.dir / f"music_plan_{prefix}.json"
    plan = read_json(plan_file)
    names = {f.name for f in files}
    if not plan or any(p["track"] not in names for p in plan.get("parts", [])):
        c.log("Музыка по сценарию: слушаю треки из «Музыки» и размечаю ролик по настроению…")
        energy = musiclib.energies()
        meta = musiclib.styles()
        lib = "\n".join(f"- {f.name} | energy {energy.get(f.name, 3)}/5"
                        + (f" | {meta[f.name]['style_ru']}" if meta.get(f.name, {}).get("style_ru") else "")
                        for f in files[:80])
        t, rows = 0.0, []
        for i, (sc, d) in enumerate(zip(scenes, durations)):
            rows.append(f"{i}. [{_clock(t)}] {sc.get('text', '')[:220]}")
            t += d
        wish = (opts.get("music_style") or c.project.get("music_style") or "").strip()
        try:
            plan = llm.call_json(
                _check_settings(c.settings), "You are the music editor of a YouTube video. Answer only with JSON.",
                "Split the video into 2-5 musical parts by the mood of the story (for example: a calm or mysterious "
                "opening, rising tension, the climax, the resolution) and pick one track from the library for each "
                "part: its energy must match the part (1 calm ... 5 driving), and its name or style the mood. A part "
                "lasts at least 40 seconds; neighbouring parts use different tracks; the first part starts at scene "
                "0. breaths: 0-3 scene numbers right before a big reveal or turn where the music should fall silent "
                "for that one scene, so the words land; leave it empty if no moment deserves it. mood_ru: the mood "
                "of the part in Russian, 1-3 words."
                + (f"\nThe owner's wish for the music: {wish}" if wish else "")
                + f"\n\nLIBRARY (file | energy | style):\n{lib}\n\nSCENES ([time] narration):\n" + "\n".join(rows),
                SCHEMA_MUSIC, c.usage, 4000)
        except UserError as e:
            c.log(f"Музыку по сценарию разметить не получилось ({e}), ставлю один трек.")
            return None
        parts = sorted((p for p in plan.get("parts", []) if p.get("track") in names
                        and 0 <= int(p.get("from_scene", -1)) < len(scenes)), key=lambda p: p["from_scene"])
        if not parts:
            return None
        parts[0]["from_scene"] = 0
        merged = [parts[0]]
        for p in parts[1:]:  # the same track again or a part that starts on the same scene: one part
            if p["from_scene"] > merged[-1]["from_scene"] and p["track"] != merged[-1]["track"]:
                merged.append(p)
        plan = {"parts": merged, "breaths": sorted({int(b) for b in plan.get("breaths", [])
                                                     if 0 < int(b) < len(scenes) - 1})[:3]}
        write_json(plan_file, plan)
    starts = [sum(durations[:i]) for i in range(len(scenes))] + [total]
    parts = plan["parts"]
    spans = [(starts[p["from_scene"]], starts[parts[k + 1]["from_scene"]] if k + 1 < len(parts) else total + 1)
             for k, p in enumerate(parts)]
    out = clips_dir / f"music_bed_{prefix}.wav"
    args, chains = [], []
    for k, (p, (a, b)) in enumerate(zip(parts, spans)):
        length = b - a + (XFADE if k + 1 < len(parts) else 0)
        args += ["-stream_loop", -1, "-i", musiclib.MUSIC_DIR / p["track"]]
        gain = 0.8 + 0.1 * max(1, min(5, int(p.get("energy", 3))))  # a driving part sits a bit louder
        chains.append(f"[{k}:a]atrim=0:{length:.3f},asetpts=PTS-STARTPTS,aformat=sample_rates=44100:"
                      f"channel_layouts=stereo,volume={gain:.2f}[p{k}]")
    fc = ";".join(chains)
    last = "p0"
    for k in range(1, len(parts)):
        fc += f";[{last}][p{k}]acrossfade=d={XFADE}:c1=tri:c2=tri[x{k}]"
        last = f"x{k}"
    # silent for the last 3 seconds before the turn, back softly as it is told
    holes = [(max(starts[i], starts[i + 1] - 3.0), starts[i + 1] + 0.3) for i in plan.get("breaths", [])]
    if holes:  # the music fades out over BREATH seconds before the beat and back in after it
        g = "*".join(f"(1-clip(min((t-{a - BREATH:.2f})/{BREATH}\\,({b + BREATH:.2f}-t)/{BREATH})\\,0\\,1))"
                     for a, b in holes)
        fc += f";[{last}]volume='{g}':eval=frame[bed]"
        last = "bed"
    try:
        media.run([*args, "-filter_complex", fc, "-map", f"[{last}]", "-t", f"{total + 1:.3f}", out],
                  "Музыка по сценарию")
    except UserError as e:
        c.log(f"Музыку по сценарию собрать не получилось ({e}), ставлю один трек.")
        return None
    desc = ", ".join(f"{_clock(a)} {p['mood_ru']} ({Path(p['track']).stem})" for p, (a, _) in zip(parts, spans))
    c.log(f"Музыка по сценарию: {desc}" + (f"; музыка замолкает перед поворотами: {len(holes)}" if holes else ""))
    return out


def _music_for(c: Ctx, total: float):
    """Background track: none, a fitting one from the music folder, or a new one composed for this video."""
    opts = (db.get(c.id) or {}).get("options") or c.opts
    chosen = opts.get("music_track") or ""
    mute_off = c.mute and (chosen == "none" or (not chosen and (opts.get("music_mode") or "library") == "off"))
    if mute_off:  # without voice the music is the whole sound: a silent video is never what the owner wants
        c.log("Ролик без голоса: без музыки он был бы беззвучным, поэтому беру трек из раздела «Музыка».")
        chosen, opts = "", dict(opts, music_mode="library")
    if chosen == "none":
        c.log("Музыка выключена для этого ролика.")
        return None
    if chosen:  # the owner picked the track himself on the video page
        f = musiclib.MUSIC_DIR / Path(chosen).name
        if f.is_file():
            c.log(f"Фоновая музыка (ваш выбор): {f.name}")
            return f
        c.log(f"Выбранного трека «{chosen}» больше нет в разделе «Музыка», подбираю другой.")
    mode = opts.get("music_mode") or "library"
    if mode == "off":
        c.log("Музыка выключена для этого ролика.")
        return None
    extras = c.data("extras") or {}
    # The owner's own words about the music (this video, then the project) beat the style the writer guessed.
    wish = (opts.get("music_style") or c.project.get("music_style") or "").strip()
    mood = wish or " ".join([extras.get("music_style_ru", ""), extras.get("music_prompt", "")])
    own = c.dir / "music.mp3"
    if mode == "generate" and not c.demo:
        if own.exists():
            return own
        prompt = (_music_prompt(c, wish, extras) if wish else extras.get("music_prompt")) \
            or f"Instrumental background music for a video about: {c.niche}"
        style = wish[:50] if wish else extras.get("music_style_ru") or "фоновая музыка"
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
    track = None
    if opts.get("music_picked") and (musiclib.MUSIC_DIR / opts["music_picked"]).is_file():
        track = musiclib.MUSIC_DIR / opts["music_picked"]  # a new assembly keeps the track picked before
    track = track or musiclib.pick(mood)
    if track:
        c.log(f"Фоновая музыка: {track.name}")
        if opts.get("music_picked") != track.name:
            db.update(c.id, options=dict(opts, music_picked=track.name))
    else:
        c.log("Фоновой музыки нет (раздел «Музыка» пуст), ролик будет без музыки.")
    return track


SCHEMA_CHAPTERS = {"type": "object", "properties": {"chapters": {"type": "array", "items": {"type": "object", "properties": {
    "scene": {"type": "integer"}, "title": {"type": "string"}, "title_ru": {"type": "string"}},
    "required": ["scene", "title", "title_ru"]}}}, "required": ["chapters"]}


def make_chapters(jid: str, usage: dict = None) -> list:
    """YouTube chapters of a long video: the scene where each part starts and a title phrased as the viewer's
    question. The times come from the voice, so a new voice or speed keeps the titles and moves the times."""
    d = job_dir(jid)
    scenes = (read_json(d / "scenes.json") or {}).get("scenes") or []
    if not scenes:
        raise UserError("Сцен ещё нет.")
    job = db.get(jid) or {}
    lang = job_language(job)
    rows = "\n".join(f"[{i + 1}] {sc.get('text', '')}" for i, sc in enumerate(scenes))
    user = (f"Here is the narration of a YouTube video split into numbered scenes. Split it into 4-10 chapters for the "
            f"video description so viewers can jump to the answers they want. Each chapter starts at a scene number; "
            f"the first chapter starts at scene 1 (the hook). A chapter starts where a new question or part begins, "
            f"never mid-thought, and chapters are not too short. Write each title as the question this part answers, "
            f"short (at most 60 characters), concrete and curious, in the video's language ({LANG_NAMES.get(lang, lang)}), for example "
            f"\"Why did people in Mehrgarh drill holes in teeth?\". title_ru: the same in Russian.\n\n{rows}")
    settings = _check_settings(load_settings())
    r = llm.call_json(settings, "You write YouTube chapters. Answer only with JSON.", user, SCHEMA_CHAPTERS,
                      usage if usage is not None else {}, 4000)
    items = sorted({int(x["scene"]): x for x in r.get("chapters") or [] if 1 <= int(x.get("scene") or 0) <= len(scenes)}.values(),
                   key=lambda x: x["scene"])
    if items and items[0]["scene"] != 1:
        items[0]["scene"] = 1
    write_json(d / "chapters.json", {"items": items})
    return items


def _clock(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"


def chapters_text(jid: str) -> str:
    """The chapter lines for the description (empty when there are none or YouTube would not accept them)."""
    d = job_dir(jid)
    items = (read_json(d / "chapters.json") or {}).get("items") or []
    durs = (read_json(d / "voice.json") or {}).get("durations") or []
    if not items or not durs:
        return ""
    opts = (db.get(jid) or {}).get("options") or {}
    speed = float(opts.get("speed_video") or 1) or 1
    starts = [sum(durs[:i]) / speed for i in range(len(durs))]
    lines, last = [], None
    for x in items:
        i = x["scene"] - 1
        if i >= len(starts):
            continue
        t = 0 if not lines else starts[i]
        if last is not None and t - last < 10:  # YouTube wants every chapter to be at least 10 seconds long
            continue
        lines.append(f"{_clock(t)} {x['title']}")
        last = t
    return "\n".join(lines) if len(lines) >= 3 else ""


def full_description(jid: str, extras: dict) -> str:
    desc = (extras.get("description") or "").strip()
    ch = chapters_text(jid)
    return f"{desc}\n\n{ch}".strip() if ch else desc


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
    if not c.vertical:
        _fix_title_minutes(c, r.get("duration", 0))
    if not c.vertical and not c.demo and r.get("duration", 0) >= 90 and not (c.dir / "chapters.json").exists():
        c.log("Делаю тайм-коды для описания…")
        try:
            make_chapters(c.id, c.usage)
            c.save_usage()
        except Exception as e:  # noqa: BLE001 - timestamps are an extra
            c.log(f"Тайм-коды сделать не получилось: {e}")
    if not c.vertical:
        make_package(c.id)


MINUTES_RE = re.compile(r"\b(\d{1,3})(\s*-?\s*)(minutes?|mins?|минут[ауы]?|мин|minutos?|minuten|minuti)\b", re.I)


def _fix_title_minutes(c: Ctx, seconds: float):
    """Titles like "The Entire History of X in 8 Minutes" are invented before the video exists: the number is set to
    the real length, so the title never promises 8 minutes for an 18-minute video."""
    if seconds < 60:
        return
    real = max(1, round(seconds / 60))
    ex = c.data("extras") or {}
    changed = []
    for k in ("youtube_title", "youtube_title_ru"):
        t = ex.get(k) or ""
        m = MINUTES_RE.search(t)
        if m and int(m.group(1)) != real:
            ex[k] = MINUTES_RE.sub(lambda x: f"{real}{x.group(2)}{x.group(3)}", t, count=1)
            changed.append(ex[k])
    if not changed:
        return
    write_json(c.out("extras"), ex)
    job = db.get(c.id) or {}
    if MINUTES_RE.search(job.get("title") or ""):
        db.update(c.id, title=MINUTES_RE.sub(lambda x: f"{real}{x.group(2)}{x.group(3)}", job["title"], count=1))
    c.log(f"Число минут в названии поправлено под настоящую длину ролика: {changed[0]}")


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
    if kind == "video":
        make_package(jid)  # the timestamps in the description follow the speed
    export(jid)
    return {"speed": speed, "duration": info.get("duration")}


def export(jid: str, log=None):
    """Puts the finished files into ~/Movies/Авто ютуб/<project>/<video>/ (see exporter.py); never fails the job."""
    from . import exporter
    try:
        f = exporter.export_job(jid)
        if f and log:
            log(f"Файлы ролика лежат в папке: {f}")
    except Exception as e:  # noqa: BLE001
        if log:
            log(f"Не удалось сложить файлы в папку ролика: {e}")


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


# Several videos are made at once, but the assembly (ffmpeg) loads the whole Mac: one video at a time.
RENDER_GATE = threading.Semaphore(1)
# A topic is picked one video at a time, so videos of one project started together never get the same topic.
IDEA_GATE = threading.Lock()
GATED = {"video": RENDER_GATE, "short_video": RENDER_GATE, "idea": IDEA_GATE}


def run_job(job: dict):
    """Runs all unfinished stages. Returns the final status."""
    c = Ctx(job)
    for sid in STAGE_IDS:
        if not c.stage_enabled(sid) or c.out(sid).exists():
            continue
        c.check()
        c.stage = sid
        c.progress(0)
        gate = GATED.get(sid)
        if gate and not gate.acquire(blocking=False):
            c.log("Жду, пока соберётся другой ролик: монтаж идёт по одному…" if gate is RENDER_GATE
                  else "Жду, пока другой ролик выберет тему, чтобы темы не повторились…")
            while not gate.acquire(timeout=2):
                c.check()
        try:
            STAGE_FUNCS[sid](c)
        finally:
            if gate:
                gate.release()
        c.log(f"✓ {STAGE_NAMES[sid]}")
    c.stage = "done"
    db.update(c.id, progress=100, stage="done")
    export(c.id, c.log)
    cost = c.usage.get("llm_cost", 0) + c.usage.get("image_cost", 0) + c.usage.get("anim_cost", 0)
    c.log(f"Готово. Примерная стоимость: ${cost:.2f} + {int(c.usage.get('tts_chars', 0))} символов ElevenLabs")


# What has to be thrown away when a stage is redone (paths relative to the job folder).
# Text stages up to "scenes" change everything after them; later stages only touch what depends on them.
RESET_PLAN = {
    "extras": ["extras.json", "images/thumbnail_src.png", "images.json", "output/youtube.txt",
               "output/thumbnail.jpg", "output/thumbnail_clean.png", "output/thumbnail.png", "thumb.json"],
    "short_script": ["short_script.json", "graphics_short.json", "anim/short_*", "animate.json", "audio/short_*", "short_narration.wav", "voice.json", "images/short_*",
                     "images.json", "clips_short", "short_video.json", "output/short.mp4"],
    "approval": ["approval.json"],
    "voice": ["voice.json", "audio", "narration.wav", "short_narration.wav", "animate.json", "video.json", "clips",
              "output/video.mp4", "short_video.json", "clips_short", "output/short.mp4"],
    "images": ["images.json", "images", "take.json", "anim", "animate.json", "output/thumbnail.jpg", "output/thumbnail_clean.png", "video.json",
               "clips", "output/video.mp4", "short_video.json", "clips_short", "output/short.mp4"],
    "animate": ["animate.json", "anim", "video.json", "clips", "output/video.mp4", "short_video.json", "clips_short",
                "output/short.mp4"],
    "video": ["video.json", "clips", "output/video.mp4", "subtitles.ass", "output/subtitles.srt", "output/video_orig.mp4"],
    "short_video": ["short_video.json", "clips_short", "output/short.mp4", "short_subtitles.ass", "output/short_subtitles.srt", "output/short_orig.mp4"],
}


def _remove(d: Path, pattern: str):
    for p in d.glob(pattern):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def _scene_key(sc: dict) -> str:
    return (sc.get("speaker") or "") + "|" + re.sub(r"\s+", " ", sc.get("text") or "").strip().lower()


def _stash_pictures(d: Path):
    """Before the text is made again: keep the scene pictures and animations with their scene texts in reuse/."""
    scenes = (read_json(d / "scenes.json") or {}).get("scenes") or []
    pics = sorted((d / "images").glob("scene_*.png")) if (d / "images").exists() else []
    if not scenes or not pics:
        return  # nothing new to keep: an earlier stash, if any, stays
    keep = d / "reuse"
    shutil.rmtree(keep, ignore_errors=True)
    keep.mkdir()
    write_json(keep / "scenes.json", {"scenes": scenes,
                                      "animated": (read_json(d / "animate.json") or {}).get("scene") or []})
    for f in pics + sorted((d / "anim").glob("scene_*.mp4") if (d / "anim").exists() else []):
        shutil.move(str(f), keep / f.name)


def _reuse_pictures(c: "Ctx", scenes: list) -> list:
    """Scenes with the same text as before the edit get their old description, picture and animation.
    Returns [(new index, old index)]."""
    keep = c.dir / "reuse"
    old = read_json(keep / "scenes.json") or {}
    if not old.get("scenes"):
        return []
    free = {}
    for j, sc in enumerate(old["scenes"]):
        if (keep / f"scene_{j:03d}.png").exists():
            free.setdefault(_scene_key(sc), []).append(j)
    pairs = []
    img, adir = c.dir / "images", c.dir / "anim"
    for i, sc in enumerate(scenes):
        js = free.get(_scene_key(sc))
        if not js:
            continue
        j = js.pop(0)
        scenes[i] = {**old["scenes"][j], "text": sc["text"]}
        img.mkdir(exist_ok=True)
        shutil.copy(keep / f"scene_{j:03d}.png", img / f"scene_{i:03d}.png")
        if (keep / f"scene_{j:03d}.mp4").exists():
            adir.mkdir(exist_ok=True)
            shutil.copy(keep / f"scene_{j:03d}.mp4", adir / f"scene_{i:03d}.mp4")
        pairs.append((i, j))
    # Scenes the owner chose to animate move with their pictures.
    job = db.get(c.id) or {}
    chosen = anim_scenes(job)
    if chosen and pairs:
        moved = {j: i for i, j in pairs}
        db.update(c.id, options=dict(job["options"], anim_scenes=[moved[j] for j in chosen if j in moved]))
    shutil.rmtree(keep, ignore_errors=True)
    return pairs


def reset_from(jid: str, stage: str, keep_pictures: bool = False):
    """Delete the results of `stage` and whatever depends on it. keep_pictures: the text changes (the owner's edit or
    a stage before the scenes), so the pictures of scenes whose text stays the same are kept for reuse. A redo of
    the scenes themselves means the owner wants new pictures."""
    d = job_dir(jid)
    idx = STAGE_IDS.index(stage)
    if stage in RESET_PLAN:
        for pattern in RESET_PLAN[stage]:
            _remove(d, pattern)
    else:
        # New text means new voice, images and clips. The pictures are kept aside first: a scene whose text did not
        # change gets its old picture (and animation) back instead of a paid new one.
        if keep_pictures or idx < STAGE_IDS.index("scenes"):
            _stash_pictures(d)
        else:
            shutil.rmtree(d / "reuse", ignore_errors=True)
        for sid in STAGE_IDS[idx:]:
            (d / f"{sid}.json").unlink(missing_ok=True)
        (d / "review_progress.json").unlink(missing_ok=True)
        for rel in ("audio", "images", "take.json", "anim", "clips", "clips_short", "output", "narration.wav", "short_narration.wav",
                    "thumb.json", "graphics_*.json", "chapters.json"):
            _remove(d, rel)
    if not (d / "images").exists():
        # The pictures are gone, the owner's own ones too: forget their marks and stop for new uploads again.
        (d / "own_images.json").unlink(missing_ok=True)
        job = db.get(jid) or {}
        if (job.get("options") or {}).get("images_picked"):
            db.update(jid, options=dict(job["options"], images_picked=False))
    if stage not in RESET_PLAN or stage == "approval":
        db.update(jid, approved=0)


def _drop_scene_outputs(d: Path, kind: str, index: int):
    """Everything made from one scene picture: its clip, animation and the assembled video."""
    prefix = "short" if kind == "short" else "scene"
    (d / "anim" / f"{prefix}_{index:03d}.mp4").unlink(missing_ok=True)
    (d / "animate.json").unlink(missing_ok=True)
    own = read_json(d / "own_clips.json", []) or []
    if f"{kind}:{index}" in own:
        write_json(d / "own_clips.json", [x for x in own if x != f"{kind}:{index}"])
    (d / ("clips_short" if kind == "short" else "clips") / f"{prefix}_{index:03d}.mp4").unlink(missing_ok=True)
    (d / "images.json").unlink(missing_ok=True)
    (d / ("short_video.json" if kind == "short" else "video.json")).unlink(missing_ok=True)
    (d / "output" / ("short.mp4" if kind == "short" else "video.mp4")).unlink(missing_ok=True)


def prepare_montage(jid: str, title: str, script: str):
    """Montage only: the owner's own text is the final script. The writing stages are marked done, so the
    program goes straight to scenes, voice, pictures (his own ones first) and assembly."""
    d = job_dir(jid)
    job = db.get(jid) or {}
    opts = job.get("options") or {}
    text = re.sub(r"\n{3,}", "\n\n", script.replace("\r", "")).strip()
    if word_count(text) < 3:
        raise UserError("Вставьте текст сценария.")
    title = (title or "").strip() or re.sub(r"^[^:\n]{1,40}:\s*", "", text.split("\n")[0])[:70]
    lang = opts.get("language") or "ru"
    wpm = WPM.get(lang, WORDS_PER_MIN)
    checks = script_checks(text, word_count(text), wpm)
    write_json(d / "idea.json", {"ideas": [{"title": title, "title_ru": title, "premise": text[:300], "premise_ru": text[:300],
                                            "total": 0}], "chosen_index": 0, "montage": True})
    write_json(d / "outline.json", {"title": title, "locations": [], "characters": [], "sections": []})
    write_json(d / "script.json", {"script": text, "checks": checks})
    write_json(d / "review.json", {"rounds": [], "final_script": text, "final_overall": 0, "passed": True,
                                   "checks": checks, "edited_by_owner": True, "montage": True})
    (d / "script_final.txt").write_text(text, "utf-8")
    write_json(d / "approval.json", {"approved_at": time.time(), "montage": True})
    db.update(jid, title=title, options=dict(opts, title_ru=title, montage=True))


def scene_prompts(jid: str) -> dict:
    """Ready prompts to draw the scene pictures in another service: style first, no text in the picture."""
    d = job_dir(jid)
    job = db.get(jid) or {}
    c = Ctx(job)
    style = (c.settings.get("image_style") or "").strip().rstrip(".")
    out = {}
    for kind, name, vertical in (("video", "scenes.json", c.vertical), ("short", "short_script.json", True)):
        scenes = (read_json(d / name) or {}).get("scenes") or []
        out[kind] = [(f"{style}. " if style else "") + f"{(x.get('prompt') or '').strip().rstrip('.')}. "
                     + ("Vertical 9:16 picture" if vertical else "Horizontal 16:9 picture")
                     + ", full-bleed scene filling the whole frame. No text, no letters, no captions, no watermark."
                     for x in scenes]
    return out


def own_image(jid: str, kind: str, index: int, raw: bytes):
    """The owner's own picture for one scene: cropped to fill the frame, then the video is assembled with it."""
    from PIL import Image, ImageOps
    import io
    d = job_dir(jid)
    job = db.get(jid) or {}
    if kind == "thumbnail":  # the owner's own picture under the thumbnail headline
        try:
            im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
        except Exception:
            raise UserError("Это не картинка или формат не поддерживается. Подойдут JPG, PNG или WEBP.")
        vertical = (job.get("options") or {}).get("format") == "short"
        out = d / "images" / "thumbnail_src.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        ImageOps.fit(im, (1080, 1920) if vertical else (1920, 1080), Image.LANCZOS).save(out)
        make_package(jid)
        return
    if kind == "short":
        scenes = (read_json(d / "short_script.json") or {}).get("scenes") or []
        vertical = True
    else:
        scenes = (read_json(d / "scenes.json") or {}).get("scenes") or []
        vertical = (job.get("options") or {}).get("format") == "short"
    if not 0 <= index < len(scenes):
        raise UserError(f"Сцены {index + 1} нет: в этом ролике {len(scenes)} сцен.")
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    except Exception:
        raise UserError("Это не картинка или формат не поддерживается. Подойдут JPG, PNG или WEBP.")
    size = (1080, 1920) if vertical else (1920, 1080)
    im = ImageOps.fit(im, size, Image.LANCZOS)
    out = d / "images" / f"{'short' if kind == 'short' else 'scene'}_{index:03d}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    im.save(out)
    _drop_scene_outputs(d, kind, index)
    own = read_json(d / "own_images.json", []) or []
    key = f"{kind}:{index}"
    if key not in own:
        write_json(d / "own_images.json", own + [key])


def own_clip(jid: str, kind: str, index: int, raw: bytes, suffix: str = ".mp4"):
    """The owner's own animated clip for a scene (e.g. its picture animated in another service): it replaces the
    animation of that scene, nothing is paid. The clip is fitted to the scene length when the video is assembled."""
    d = job_dir(jid)
    if kind == "short":
        scenes = (read_json(d / "short_script.json") or {}).get("scenes") or []
    else:
        scenes = (read_json(d / "scenes.json") or {}).get("scenes") or []
    if not 0 <= index < len(scenes):
        raise UserError(f"Сцены {index + 1} нет: в этом ролике {len(scenes)} сцен.")
    prefix = "short" if kind == "short" else "scene"
    adir = d / "anim"
    adir.mkdir(exist_ok=True)
    ext = suffix if re.fullmatch(r"\.[a-z0-9]{2,5}", suffix) else ".mp4"
    src = adir / f"upload_{prefix}_{index:03d}{ext}"
    src.write_bytes(raw)
    out = adir / f"{prefix}_{index:03d}.mp4"
    tmp = adir / f"{prefix}_{index:03d}.part.mp4"
    try:
        media.video_duration(src)
        # Any format and size in, one plain H.264 clip out (no sound: the voice and music are laid on top).
        media.run(["-i", src, "-an", "-vf", "fps=30,scale=1920:1920:force_original_aspect_ratio=decrease,"
                   "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", 18, tmp], "Ваш клип")
    except UserError:
        tmp.unlink(missing_ok=True)
        raise UserError("Это не видео или формат не поддерживается. Подойдут MP4, MOV или WEBM.")
    finally:
        src.unlink(missing_ok=True)
    tmp.replace(out)
    (d / ("clips_short" if kind == "short" else "clips") / f"{prefix}_{index:03d}.mp4").unlink(missing_ok=True)
    (d / ("short_video.json" if kind == "short" else "video.json")).unlink(missing_ok=True)
    (d / "output" / ("short.mp4" if kind == "short" else "video.mp4")).unlink(missing_ok=True)
    own = read_json(d / "own_clips.json", []) or []
    if f"{kind}:{index}" not in own:
        write_json(d / "own_clips.json", own + [f"{kind}:{index}"])


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
        sc = scenes[index]
        if prompt != sc.get("prompt"):
            # Characters and items named in the new description get their reference photos; removed ones lose them.
            chars = read_json(d / "characters.json", []) or []
            its = read_json(d / "items.json", []) or []
            sc["characters"] = [ch.get("name") for ch in chars
                                if any(n and _named_in(n, prompt) for n in (ch.get("name"), ch.get("name_en")))]
            sc["items"] = [it.get("name") for it in its
                           if any(n and _named_in(n, prompt) for n in (it.get("name"), it.get("name_en")))]
        sc["prompt"] = prompt
        write_json(d / ("short_script.json" if kind == "short" else "scenes.json"), data)
    prefix = "short" if kind == "short" else "scene"
    (d / "images" / f"{prefix}_{index:03d}.png").unlink(missing_ok=True)
    _drop_scene_outputs(d, kind, index)
    own = read_json(d / "own_images.json", []) or []
    if f"{kind}:{index}" in own:
        write_json(d / "own_images.json", [x for x in own if x != f"{kind}:{index}"])


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


# -------- a version of a finished video in another language

LANG_NAMES = {"en": "English", "ru": "Russian", "de": "German", "es": "Spanish", "fr": "French"}
# The line under a Short's title that sends viewers to the long video.
SHORT_MORE = {"ru": "Полная история на нашем канале.", "en": "The full story is on our channel.",
              "de": "Die ganze Geschichte gibt es auf unserem Kanal.", "es": "La historia completa está en nuestro canal.",
              "fr": "L'histoire complète est sur notre chaîne."}
LANG_RU = {"en": "английский", "ru": "русский", "de": "немецкий", "es": "испанский", "fr": "французский"}
SCHEMA_TRANSLATION = {"type": "object", "properties": {
    "title": {"type": "string"}, "lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["title", "lines"]}
# A mirrored picture with a camera move in the other direction reads as a new shot.
MIRROR_MOTION = {"zoom_in": "zoom_out", "zoom_out": "zoom_in", "pan_left": "pan_right", "pan_right": "pan_left"}


def _translate_lines(c: Ctx, title: str, lines: list) -> tuple:
    """(title, lines) in the job's language, one line per scene, so every picture keeps its words."""
    target = LANG_NAMES.get(c.lang, c.lang)
    system = (f"You are a top YouTube localizer. You adapt videos (usually Russian or English) for a native {target}-speaking audience "
              "so they sound as if written in that language from the start: natural spoken phrasing, local idioms, "
              "jokes and slang that land the same way, numbers and units the way a native narrator says them. "
              "Keep the meaning, the hook, the twist and the punchline. Answer only with JSON."
              + c.char_block() + "\n\nChannel niche (may be in Russian): " + c.niche[:2000])
    out = []
    new_title = title
    for start in range(0, len(lines), 60):
        part = lines[start:start + 60]
        listing = "\n".join(f"{i}. {t}" for i, t in enumerate(part))
        user = (f"Translate the video below into {target}. Each numbered line is the narration of one picture: "
                f"return exactly {len(part)} lines in the same order, line i for picture i, never merge or split "
                "lines, keep each one about as long when spoken. A character's manner of speech stays recognisable "
                "(use slang of the same strength in the new language). Spoken text only: numbers in words.\n"
                + (f"Also translate the title into a strong, clickable {target} YouTube title (max 70 characters).\n"
                   f"TITLE: {title}\n" if start == 0 else "Return the title unchanged.\n")
                + "\nLINES:\n" + listing)
        for attempt in range(2):
            r = llm.call_json(c.settings, system, user, SCHEMA_TRANSLATION, c.usage, 16000)
            got = [str(x).strip() for x in r.get("lines") or []]
            if len(got) == len(part) and all(got):
                break
        else:
            raise UserError(f"Перевод вернул {len(got)} строк вместо {len(part)}. Запустите шаг ещё раз.")
        if start == 0:
            new_title = r.get("title") or title
        out += got
        c.save_usage()
    return new_title, out


def _translated(c: Ctx, scenes: list, lines: list) -> list:
    res = []
    for sc, text in zip(scenes, lines):
        sc = {**sc, "text": text}
        if sc.get("motion") in MIRROR_MOTION and c.opts.get("restyle"):
            sc["motion"] = MIRROR_MOTION[sc["motion"]]
        res.append(sc)
    return res


def _script_of(scenes: list) -> str:
    paras, cur = [], []
    for sc in scenes:
        cur.append(sc["text"])
        if sc.get("paragraph_end"):
            paras.append(" ".join(cur))
            cur = []
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras)


def _translate_scenes(c: Ctx):
    src = read_json(job_dir(c.opts["translate_from"]) / "scenes.json") or {}
    scenes = src.get("scenes") or []
    if not scenes:
        raise UserError("В исходном ролике нет сцен: перевести нечего.")
    target = LANG_NAMES.get(c.lang, c.lang)
    c.log(f"Перевожу {len(scenes)} сцен на {LANG_RU.get(c.lang, target)}…")
    if c.demo:
        title, lines = "Demo", [f"Line {i + 1} in {target}." for i in range(len(scenes))]
    else:
        title, lines = _translate_lines(c, (c.data("outline") or {}).get("title", ""), [sc["text"] for sc in scenes])
    new = _translated(c, scenes, lines)
    script = _script_of(new)
    review = c.data("review") or {}
    review.update(final_script=script, checks=script_checks(script, word_count(script), c.wpm))
    write_json(c.out("review"), review)
    write_json(c.out("script"), {**(c.data("script") or {}), "script": script})
    outline = c.data("outline") or {}
    outline["title"] = title
    write_json(c.out("outline"), outline)
    db.update(c.id, title=title)
    write_json(c.out("scenes"), {"scenes": new})


def _translate_short(c: Ctx):
    src = read_json(job_dir(c.opts["translate_from"]) / "short_script.json") or {}
    scenes = src.get("scenes") or []
    if not scenes:
        raise UserError("В исходном ролике нет шортса: перевести нечего.")
    c.log("Перевожу шортс…")
    if c.demo:
        title, lines = "Demo short", [f"Short line {i + 1}." for i in range(len(scenes))]
    else:
        title, lines = _translate_lines(c, src.get("title", ""), [sc["text"] for sc in scenes])
    new = _translated(c, scenes, lines)
    text = " ".join(sc["text"] for sc in new)
    write_json(c.out("short_script"), {**src, "title": title, "title_ru": src.get("title", ""), "script": text,
                                       "script_ru": src.get("script", ""), "scenes": new,
                                       "checks": script_checks(text, word_count(text), c.wpm)})


TRANSLATE_COPY = ("idea.json", "outline.json", "script.json", "review.json", "approval.json", "characters.json",
                  "items.json", "animate.json", "chars", "items", "images", "anim")


def _mirror_image(path: Path):
    from PIL import Image, ImageOps
    with Image.open(path) as im:
        ImageOps.mirror(im).save(path)


def make_translation(jid: str, lang: str = "en", voice_id: str = "", voice_name: str = "", channel_id: str = "",
                     restyle: bool = True, redraw: int = 0) -> dict:
    """A new video from a finished one: the same pictures and animation, a translated script, a new voice,
    subtitles, title, description and thumbnail. restyle mirrors the pictures (except ones with products, whose
    labels would read backwards) and turns the camera moves around; redraw draws the first N pictures anew."""
    src = db.get(jid)
    if not src:
        raise UserError("Ролик не найден.")
    sd = job_dir(jid)
    scenes = (read_json(sd / "scenes.json") or {}).get("scenes")
    if not scenes or not (sd / "images.json").exists():
        raise UserError("Версию на другом языке можно сделать, когда у ролика готовы картинки.")
    o = dict(src["options"])
    for k in ("episode", "title_ru", "idea_picked", "anim_picked", "translate_from", "flipped", "voice_id", "voice_name"):
        o.pop(k, None)
    o.update(language=lang, translate_from=jid, review_pause=False, idea_pause=False, anim_pause=False, images_pause=False,
             anim_picked=True, channel_id=channel_id, voice_id=voice_id, voice_name=voice_name, restyle=bool(restyle))
    short_ok = o.get("format") != "short" and o.get("make_short") and (sd / "short_script.json").exists()
    o["make_short"] = bool(short_ok)
    job = db.create(src.get("topic", ""), o, status="paused")  # the worker must not start it before the files are copied
    d = job_dir(job["id"])
    for name in TRANSLATE_COPY:
        p = sd / name
        if p.is_dir():
            shutil.copytree(p, d / name, dirs_exist_ok=True)
        elif p.exists():
            shutil.copy(p, d / name)
    img = d / "images"
    (img / "thumbnail_src.png").unlink(missing_ok=True)  # a new thumbnail is drawn for the new title
    redraw = max(0, min(int(redraw or 0), len(scenes)))
    for i in range(redraw):  # drawn anew from the same scene description: a fresh picture, no old animation
        (img / f"scene_{i:03d}.png").unlink(missing_ok=True)
        (d / "anim" / f"scene_{i:03d}.mp4").unlink(missing_ok=True)
    flipped = []
    if restyle:
        kinds = [("scene", scenes)]
        if short_ok:
            kinds.append(("short", (read_json(sd / "short_script.json") or {}).get("scenes") or []))
        for prefix, scs in kinds:
            for i, sc in enumerate(scs):
                if sc.get("items"):
                    continue
                pic, clip = img / f"{prefix}_{i:03d}.png", d / "anim" / f"{prefix}_{i:03d}.mp4"
                if pic.exists():
                    _mirror_image(pic)
                if clip.exists():
                    tmp = clip.with_suffix(".flip.mp4")
                    media.run(["-i", clip, "-vf", "hflip", "-c:a", "copy", tmp], "Отражение анимации")
                    tmp.replace(clip)
                flipped.append(f"{prefix}_{i:03d}")
    if not short_ok:
        a = read_json(d / "animate.json") or {}
        if a.get("short"):
            a["short"] = []
            write_json(d / "animate.json", a)
    o["flipped"] = flipped
    db.update(job["id"], options=o, title=src.get("title", ""), status="queued")
    return db.get(job["id"])


def retempo(jid: str):
    """A new speech tempo: the voice is rebuilt from the clips ElevenLabs already made (free) and the video reassembled."""
    audio = job_dir(jid) / "audio"
    keep = job_dir(jid) / "audio_keep"
    shutil.rmtree(keep, ignore_errors=True)
    if audio.is_dir():
        keep.mkdir()
        for f in audio.glob("*.mp3"):
            f.replace(keep / f.name)
    reset_from(jid, "voice")
    if keep.is_dir():
        audio.mkdir(exist_ok=True)
        for f in keep.glob("*.mp3"):
            f.replace(audio / f.name)
        shutil.rmtree(keep, ignore_errors=True)
