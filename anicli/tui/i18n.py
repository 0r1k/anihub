"""Minimal gettext-like localization.

English source strings are the message keys, so English needs no catalog and a missing
translation falls back to English. Use `t("Text {name}", name=...)` for formatted messages.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from textual.binding import Binding
from textual.dom import DOMNode

DEFAULT_LANGUAGE = "en"
LANGUAGES = {"en": "English", "uk": "Українська"}

UK: dict[str, str] = {
    # app
    "Settings": "Налаштування",
    "Stop": "Стоп",
    "Quit": "Вихід",
    "Playback error: {error}": "Помилка відтворення: {error}",
    "Update available": "Доступне оновлення",
    "Run `anihub update` to upgrade": "Оновіть: anihub update",
    "Update anihub": "Оновити anihub",
    "F9 — update now": "F9 — оновити зараз",
    "Update anihub {current} → {latest}? anihub will close and the update will run in the terminal.": (
        "Оновити anihub {current} → {latest}? anihub закриється, оновлення піде в терміналі."
    ),
    "Update": "Оновити",
    "Later": "Пізніше",
    "Player not found: {command}. Choose another one in settings (F2)": (
        "Плеєр не знайдено: {command}. Оберіть інший у налаштуваннях (F2)"
    ),
    # home
    "Search": "Пошук",
    "Ongoing": "Онгоінги",
    "History": "Історія",
    "Refresh": "Оновити",
    "Delete from history": "Видалити з історії",
    "Anime title, Enter to search": "Назва аніме, Enter — шукати",
    "Type a query and press Enter": "Введіть запит і натисніть Enter",
    "No ongoing (is the site down?)": "Немає онгоінгів (сайт недоступний?)",
    "History is empty": "Історія порожня",
    "Search error: {error}": "Помилка пошуку: {error}",
    "Nothing found for «{query}»": "За запитом «{query}» нічого не знайдено",
    "Results: {count}": "Результатів: {count}",
    "Failed to load ongoing: {error}": "Не вдалося завантажити онгоінги: {error}",
    "Continue «{episode}» from {time}?": "Продовжити «{episode}» з {time}?",
    "Continue": "Продовжити",
    "To episodes": "До серій",
    # anime
    "Back": "Назад",
    "All episodes": "Усі серії",
    "Clear marks": "Зняти позначки",
    "Range": "Діапазон",
    "Choose": "Обрати",
    "Empty": "Порожньо",
    "Episodes: 1-12 or 1 3 5-7, Enter to mark": "Серії: 1-12 або 1 3 5-7, Enter — позначити",
    "Select an episode on the left": "Оберіть серію ліворуч",
    "Episodes": "Серії",
    "watched {count}": "переглянуто {count}",
    "marked {count}": "позначено {count}",
    "leak hidden: {episodes} (F2)": "приховано злив: {episodes} (F2)",
    "Space to mark, Enter to choose": "Пробіл — позначити, Enter — обрати",
    "Dub / player": "Озвучка / плеєр",
    "Episode": "Серія",
    "Failed to load the title: {error}": "Не вдалося завантажити тайтл: {error}",
    "No episodes available": "У тайтла немає доступних серій",
    "New episodes: {count}": "Нових серій: {count}",
    "No new episodes": "Нових серій немає",
    "Failed to load sources: {error}": "Не вдалося завантажити джерела: {error}",
    "No sources available": "Немає доступних джерел",
    "episode {num}": "серія {num}",
    "{count} episodes from {num}": "{count} серій, починаючи з {num}",
    "{title}: loading {count} episodes…": "{title}: завантаження {count} серій…",
    "{title}: getting the video from {player}…": "{title}: отримую відео від {player}…",
    "Played {played} of {total}: «{source}» has no next episodes": (
        "Відтворено {played} з {total}: у «{source}» немає наступних серій"
    ),
    "Playback stopped": "Відтворення зупинено",
    "Play the next episodes starting from «{title}»?": "Відтворити наступні серії, починаючи з «{title}»?",
    "Play": "Дивитися",
    # playback
    "Player {player} («{source}») failed to return video: {error}. Try another player": (
        "Плеєр {player} («{source}») не віддав відео: {error}. Спробуйте інший плеєр"
    ),
    "Player {player} («{source}») returned no video. Try another player": (
        "Плеєр {player} («{source}») не повернув відео. Спробуйте інший плеєр"
    ),
    "No saved sources for this title": "Для тайтла немає збережених джерел",
    # settings
    "Cancel": "Скасувати",
    "Language": "Мова",
    "Source": "Джерело",
    "Quality (or the closest available)": "Якість (або найближча доступна)",
    "Player": "Плеєр",
    "Other (command)": "Інший (команда)",
    "Player command": "Команда плеєра",
    "Extra player arguments": "Додаткові аргументи плеєра",
    "Load video through anihub (fast start, reconnects)": ("Відео через anihub (швидкий старт, перепідключення)"),
    "Episodes per player playlist": "Серій в одному плейлисті плеєра",
    "Proxy": "Проксі",
    "format: scheme://user:password@host:port": "формат: scheme://user:password@host:port",
    "HTTP timeout, seconds": "Тайм-аут HTTP, секунд",
    "Fix the highlighted fields": "Виправте виділені поля",
    "Settings applied but not saved: {error}": "Налаштування застосовано, але не збережено: {error}",
    # dialogs
    "Yes": "Так",
    "No": "Ні",
    # MyAnimeList
    "Watching": "Дивлюся",
    "Completed": "Переглянуто",
    "On-Hold": "Відкладено",
    "Dropped": "Покинуто",
    "Plan to Watch": "Заплановано",
    "Not in list": "Немає у списку",
    "All": "Усі",
    "No score": "Без оцінки",
    "Masterpiece": "Шедевр",
    "Great": "Чудово",
    "Very Good": "Дуже добре",
    "Good": "Добре",
    "Fine": "Непогано",
    "Average": "Середньо",
    "Bad": "Погано",
    "Very Bad": "Дуже погано",
    "Horrible": "Жахливо",
    "Appalling": "Огидно",
    "{progress} ep.": "{progress} еп.",
    "Rate": "Оцінити",
    "MAL status": "Статус MAL",
    "Watched episodes": "Переглянуто серій",
    "MAL link": "Прив'язка MAL",
    "Remove from MAL": "Видалити з MAL",
    "Link": "Прив'язати",
    "Remove": "Видалити",
    "MyAnimeList: searching…": "MyAnimeList: пошук…",
    "MyAnimeList: error": "MyAnimeList: помилка",
    "Not linked to MyAnimeList: press l to choose": "Не прив'язано до MyAnimeList: натисніть l, щоб обрати",
    "MyAnimeList updated: {summary}": "MyAnimeList оновлено: {summary}",
    "«{title}» removed from your MyAnimeList": "«{title}» видалено з вашого MyAnimeList",
    "Remove «{title}» from your MyAnimeList? The score and progress will be lost.": (
        "Видалити «{title}» з вашого MyAnimeList? Оцінку та прогрес буде втрачено."
    ),
    "Mark «{title}» as watched on MyAnimeList?": "Позначити «{title}» переглянутою на MyAnimeList?",
    "The episode is not counted on MyAnimeList: link the title first (l on the title page)": (
        "Серію не зараховано на MyAnimeList: спочатку прив'яжіть тайтл (l на сторінці тайтла)"
    ),
    "{error}. Log in again in settings (F2)": "{error}. Увійдіть знову в налаштуваннях (F2)",
    "You have watched «{title}» to the end. Rate it on MyAnimeList?": (
        "Ви додивилися «{title}» до кінця. Поставити оцінку на MyAnimeList?"
    ),
    "Esc — later": "Esc — пізніше",
    "Log in to MyAnimeList in settings (F2)": "Увійдіть у MyAnimeList в налаштуваннях (F2)",
    "No titles with this status": "Немає тайтлів з цим статусом",
    "Pick «{title}» in the search results: it will be linked to MyAnimeList": (
        "Оберіть «{title}» у результатах пошуку: тайтл прив'яжеться до MyAnimeList"
    ),
    "Rate «{title}»": "Оцінка «{title}»",
    "Digits 1-9, 0 for 10": "Цифри 1-9, 0 — це 10",
    "Watched episodes of «{title}»": "Переглянуто серій «{title}»",
    "of {total}": "з {total}",
    "of {total}, Enter to save": "з {total}, Enter — зберегти",
    "Find the title on MyAnimeList": "Знайти тайтл на MyAnimeList",
    "Original or english title, Enter to search": "Оригінальна або англійська назва, Enter — шукати",
    "At least 3 characters": "Щонайменше 3 символи",
    "Log in to MyAnimeList": "Вхід у MyAnimeList",
    "Or paste the address of the page you were redirected to": "Або вставте адресу сторінки, на яку вас перенаправило",
    "Open browser": "Відкрити браузер",
    "Copy link": "Скопіювати посилання",
    "Allow access in the browser, it will return you here automatically.": (
        "Дозвольте доступ у браузері, після цього ви автоматично повернетеся сюди."
    ),
    "Open this link in a browser and allow access:": "Відкрийте посилання в браузері та дозвольте доступ:",
    "Port of {url} is busy: paste the redirect address manually": (
        "Порт {url} зайнятий: вставте адресу перенаправлення вручну"
    ),
    "Could not open the browser, copy the link": "Не вдалося відкрити браузер, скопіюйте посилання",
    "Link copied": "Посилання скопійовано",
    "Create an API client at {url}: App Type «other», App Redirect URL {redirect}. Paste its Client ID here.": (
        "Створіть API-клієнт на {url}: App Type «other», App Redirect URL {redirect}. Вставте його Client ID сюди."
    ),
    "Count watched episodes and update the status": "Зараховувати переглянуті серії та змінювати статус",
    "Logged in as {name}": "Вхід виконано: {name}",
    "Log out": "Вийти",
    "Not logged in": "Вхід не виконано",
    "Log in": "Увійти",
    "Enter the Client ID first": "Спочатку вкажіть Client ID",
    "Show posters on the title page": "Показувати постери на сторінці тайтла",
    "The terminal draws no images (kitty graphics protocol or sixel): posters are not shown": (
        "Термінал не малює зображень (kitty graphics protocol чи sixel): постери не показуються"
    ),
    "Subtitles from other sites": "Субтитри з інших сайтів",
    "SUB": "СУБ",
    "loading subtitles from other sites…": "завантаження субтитрів з інших сайтів…",
    "no subtitles: t — search other sites": "немає субтитрів: t — шукати на інших сайтах",
    "Searching «{title}» on other sites…": "Шукаю «{title}» на інших сайтах…",
    "Found on: {sites}": "Знайдено: {sites}",
    "Other sites": "Інші сайти",
    "«{title}» on other sites": "«{title}» на інших сайтах",
    "Enter — open the title on this site": "Enter — відкрити тайтл на цьому сайті",
    "episodes: {count}": "серій: {count}",
    "episode {num}: {players}": "серія {num}: {players}",
    "Searching: {answered} of {total} sites answered": "Пошук: відповіли {answered} з {total} сайтів",
    "Found on {found} of {total} sites": "Знайдено на {found} з {total} сайтів",
    "searching…": "шукаю…",
    "not found": "не знайдено",
    "similar name, check the season": "схожа назва, перевірте сезон",
    "The title is not found on other sites": "Тайтл не знайдено на інших сайтах",
    "Choose an episode: subtitles from these sites are added to the list": (
        "Оберіть серію: субтитри з цих сайтів додадуться до списку"
    ),
    "Subtitles first in the dub list": "Субтитри першими у списку озвучок",
    "Show leaked episodes (12 when only 1-6 are out)": "Показувати злиті серії (12-та, коли вийшли 1-6)",
    "Fields with errors were not saved": "Поля з помилками не збережено",
    "Close": "Закрити",
    "Airing": "Виходять",
    "Announced": "Анонси",
    "ANNOUNCED": "АНОНС",
    "from {date}": "з {date}",
    "Find": "Знайти",
    "Clear": "Очистити",
    "Find in the list (f): any title, english or russian": (
        "Пошук у списку (f): будь-яка назва, англійська чи українська"
    ),
    "Random": "Випадкове",
    "Nothing matches «{query}»": "Нічого не знайдено за «{query}»",
    "{user} · {shown} of {total}": "{user} · {shown} з {total}",
    "With new episodes: {new} of {total}": "З новими серіями: {new} з {total}",
    "Nothing in Watching on MyAnimeList": "У списку «Дивлюся» на MyAnimeList порожньо",
    "new: {count}": "нових: {count}",
    "checking the site…": "перевіряю сайт…",
    "caught up": "все переглянуто",
    "the site did not answer": "сайт не відповів",
    "not watched in anihub yet: Enter to find it on the site": "ще не дивилися в anihub: Enter — знайти на сайті",
    "NEW": "НОВЕ",
    "new episode {ago}": "нова серія {ago}",
    "{count} min ago": "{count} хв тому",
    "{count} h ago": "{count} год тому",
    "{count} d ago": "{count} дн тому",
    "premiered {date}, no episodes on the site yet": "прем'єра {date}, серій на сайті поки немає",
    "Airing: {airing} · Announced: {announced}": "Виходять: {airing} · Анонси: {announced}",
    # MyAnimeList profile
    "Profile": "Профіль",
    "{user}'s profile": "Профіль {user}",
    "{user}'s anime list": "Список аніме {user}",
    "Anime list": "Список аніме",
    "Other user": "Інший користувач",
    "In browser": "У браузері",
    "MyAnimeList user": "Користувач MyAnimeList",
    "User name, Enter to open the profile": "Ім'я користувача, Enter — відкрити профіль",
    "Friends": "Друзі",
    "Friends: {count}": "Друзі: {count}",
    "No friends": "Друзів немає",
    "Last Anime Updates": "Останні оновлення аніме",
    "No updates yet": "Оновлень ще немає",
    "Favorites": "Улюблене",
    "No favorites yet": "Улюбленого ще немає",
    "No biography yet.": "Біографії ще немає.",
    "Anime Stats": "Статистика аніме",
    "Manga Stats": "Статистика манґи",
    "Days": "Днів",
    "Mean Score": "Середня оцінка",
    "Total Entries": "Усього записів",
    "Rewatched": "Переглянуто повторно",
    "Reread": "Перечитано",
    "Chapters": "Розділів",
    "Volumes": "Томів",
    "Reading": "Читаю",
    "Plan to Read": "У планах",
    "Last Online": "Востаннє онлайн",
    "Gender": "Стать",
    "Birthday": "День народження",
    "Location": "Місце",
    "Joined": "Реєстрація",
    "Anime": "Аніме",
    "Manga": "Манґа",
    "Character": "Персонаж",
    "Person": "Людина",
    "To Plan to Watch": "У Plan to Watch",
    "«{title}» added to Plan to Watch": "«{title}» додано в Plan to Watch",
    "«{title}» is in your list already: {status}": "«{title}» уже у вашому списку: {status}",
    "in your list: {status}": "у вашому списку: {status}",
    "not in your list: a — to Plan to Watch": "немає у вашому списку: a — у Plan to Watch",
    "The list is not available: {error}": "Список недоступний: {error}",
    # notifications: related titles not in the list
    "Notifications": "Сповіщення",
    "Hide": "Приховати",
    "Mark all as read": "Позначити все прочитаним",
    "No new related titles": "Нових пов'язаних тайтлів немає",
    "Checking related titles: {done} of {total}…": "Перевірка пов'язаних тайтлів: {done} з {total}…",
    "Related titles not in your list: {total} · new: {unread}": (
        "Пов'язані тайтли не у списку: {total} · нових: {unread}"
    ),
    "added to MyAnimeList {when}": "додано на MyAnimeList {when}",
    "Sequel": "Сиквел",
    "Prequel": "Приквел",
    "Alternative Setting": "Альтернативний сеттінг",
    "Alternative Version": "Альтернативна версія",
    "Side Story": "Побічна історія",
    "Parent Story": "Основна історія",
    "Summary": "Підсумок",
    "Full Story": "Повна історія",
    "Spin-Off": "Спін-оф",
    "Other": "Інше",
}

CATALOGS: dict[str, dict[str, str]] = {"en": {}, "uk": UK}

_language = DEFAULT_LANGUAGE
# classes with translatable binding descriptions, in definition order (parents first)
_localized: list[tuple[type[DOMNode], list[Binding]]] = []


def get_language() -> str:
    return _language


def set_language(code: str) -> None:
    """Switch language and re-translate binding descriptions of the registered classes."""
    global _language
    _language = code if code in CATALOGS else DEFAULT_LANGUAGE
    for cls, source in _localized:
        cls.BINDINGS = [replace(b, description=t(b.description)) if b.description else b for b in source]
    # merged bindings include parent bindings, so rebuild after all classes are updated
    for cls, _ in _localized:
        cls._merged_bindings = cls._merge_bindings()


def t(text: str, **kwargs: Any) -> str:
    translated = CATALOGS[_language].get(text, text)
    return translated.format(**kwargs) if kwargs else translated


def localized_bindings(cls: type[DOMNode]) -> type[DOMNode]:
    """Class decorator: BINDINGS descriptions are English source strings translated by `set_language`."""
    source = [b if isinstance(b, Binding) else Binding(*b) for b in cls.__dict__.get("BINDINGS", [])]
    _localized.append((cls, source))
    return cls
