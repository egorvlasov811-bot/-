from leadscanner.keyword_filter import KeywordFilter
from leadscanner.textnorm import similarity, strip_links, truncate

DICT = {
    "hot_ru": ["окупаем", "рекламн метк"],
    "warm_ru": ["мини-апп", "закупк рекламы"],
    "hot_en": ["bot analytics"],
    "warm_en": [],
    "minus": ["казино", "18+"],
}


def test_hot_root_matches_any_ending():
    r = KeywordFilter(DICT).check("Ребята, как вы считаете окупаемость рекламы в своём боте?")
    assert r.passed and r.hot == ["окупаем"]


def test_multiword_roots_in_sequence():
    r = KeywordFilter(DICT).check("Кто ставит рекламные метки на посевы и как потом смотрит?")
    assert r.passed and "рекламн метк" in r.hot


def test_hyphen_and_yo_and_case():
    r = KeywordFilter(DICT).check("ЗАПУСТИЛИ МИНИ-АППЫ, ещё ищем закупку рекламы для роста")
    assert r.passed and "мини-апп" in r.warm


def test_yo_is_normalized_in_dictionary_too():
    f = KeywordFilter({**DICT, "hot_ru": ["ещё нужна аналитика"]})
    assert f.check("нам ещё нужна аналитика для бота срочно и недорого").passed


def test_minus_word_blocks():
    r = KeywordFilter(DICT).check("Окупаемость казино трафика в боте, пишите в личку срочно")
    assert not r.passed and r.reason.startswith("minus")


def test_short_messages_are_rejected():
    assert KeywordFilter(DICT, min_words=6).check("окупаемость бота?").reason == "short"


def test_no_keywords():
    assert KeywordFilter(DICT).check("Всем привет, кто сегодня идёт на митап вечером?").reason == "no_keywords"


def test_english():
    assert KeywordFilter(DICT).check("Looking for bot analytics that shows which ads convert").passed


def test_similarity_and_text_helpers():
    a = "Увидел ваш вопрос про окупаемость. Как сейчас считаете?"
    assert similarity(a, a.upper()) == 1.0
    assert similarity(a, "Совсем другой текст о погоде и котиках") < 0.5
    assert strip_links("Привет https://x.com и t.me/abc и @someone") == "Привет и и"
    cut = truncate("Слово " * 200, 450)
    assert len(cut) <= 450
