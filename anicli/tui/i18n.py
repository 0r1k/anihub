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
LANGUAGES = {"en": "English", "ru": "Русский", "uk": "Українська"}

RU: dict[str, str] = {
    # app
    "Settings": "Настройки",
    "Stop": "Стоп",
    "Quit": "Выход",
    "Playback error: {error}": "Ошибка воспроизведения: {error}",
    "Update available": "Доступно обновление",
    "Run `anihub update` to upgrade": "Обновите: anihub update",
    "Player not found: {command}. Choose another one in settings (F2)": (
        "Плеер не найден: {command}. Выберите другой в настройках (F2)"
    ),
    # home
    "Search": "Поиск",
    "Ongoing": "Онгоинги",
    "History": "История",
    "Refresh": "Обновить",
    "Delete from history": "Удалить из истории",
    "Anime title, Enter to search": "Название аниме, Enter — искать",
    "Type a query and press Enter": "Введите запрос и нажмите Enter",
    "No ongoing (is the site down?)": "Нет онгоингов (сайт недоступен?)",
    "History is empty": "История пуста",
    "Search error: {error}": "Ошибка поиска: {error}",
    "Nothing found for «{query}»": "По запросу «{query}» ничего не найдено",
    "Results: {count}": "Результатов: {count}",
    "Failed to load ongoing: {error}": "Не удалось загрузить онгоинги: {error}",
    "Continue «{episode}» from {time}?": "Продолжить «{episode}» с {time}?",
    "Continue": "Продолжить",
    "To episodes": "К сериям",
    # anime
    "Back": "Назад",
    "All episodes": "Все серии",
    "Clear marks": "Снять отметки",
    "Range": "Диапазон",
    "Choose": "Выбрать",
    "Empty": "Пусто",
    "Episodes: 1-12 or 1 3 5-7, Enter to mark": "Серии: 1-12 или 1 3 5-7, Enter — отметить",
    "Select an episode on the left": "Выберите серию слева",
    "Episodes": "Серии",
    "watched {count}": "просмотрено {count}",
    "marked {count}": "отмечено {count}",
    "Space to mark, Enter to choose": "Пробел — отметить, Enter — выбрать",
    "Dub / player": "Озвучка / плеер",
    "Episode": "Серия",
    "Failed to load the title: {error}": "Не удалось загрузить тайтл: {error}",
    "No episodes available": "У тайтла нет доступных серий",
    "New episodes: {count}": "Новых серий: {count}",
    "No new episodes": "Новых серий нет",
    "Failed to load sources: {error}": "Не удалось загрузить источники: {error}",
    "No sources available": "Нет доступных источников",
    "episode {num}": "серия {num}",
    "{count} episodes from {num}": "{count} серий, начиная с {num}",
    "{title}: loading {count} episodes…": "{title}: загрузка {count} серий…",
    "{title}: getting the video from {player}…": "{title}: получаю видео от {player}…",
    "Played {played} of {total}: «{source}» has no next episodes": (
        "Воспроизведено {played} из {total}: у «{source}» нет следующих серий"
    ),
    "Playback stopped": "Воспроизведение остановлено",
    "Play the next episodes starting from «{title}»?": "Воспроизвести следующие серии, начиная с «{title}»?",
    "Play": "Смотреть",
    # playback
    "Player {player} («{source}») failed to return video: {error}. Try another player": (
        "Плеер {player} («{source}») не отдал видео: {error}. Попробуйте другой плеер"
    ),
    "Player {player} («{source}») returned no video. Try another player": (
        "Плеер {player} («{source}») не вернул видео. Попробуйте другой плеер"
    ),
    "No saved sources for this title": "Для тайтла нет сохранённых источников",
    # settings
    "Save": "Сохранить",
    "Cancel": "Отмена",
    "Language": "Язык",
    "Source": "Источник",
    "Quality (or the closest available)": "Качество (или ближайшее доступное)",
    "Player": "Плеер",
    "Other (command)": "Другой (команда)",
    "Player command": "Команда плеера",
    "Extra player arguments": "Дополнительные аргументы плеера",
    "Load video through anihub (fast start, reconnects)": (
        "Загружать видео через anihub (быстрый старт, переподключение)"
    ),
    "Episodes per player playlist": "Серий в одном плейлисте плеера",
    "Proxy": "Прокси",
    "format: scheme://user:password@host:port": "формат: scheme://user:password@host:port",
    "HTTP timeout, seconds": "Таймаут HTTP, секунд",
    "Fix the highlighted fields": "Исправьте выделенные поля",
    "Enter the player command": "Укажите команду плеера",
    "Settings applied but not saved: {error}": "Настройки применены, но не сохранены: {error}",
    # dialogs
    "Yes": "Да",
    "No": "Нет",
    # MyAnimeList
    "Watching": "Смотрю",
    "Completed": "Просмотрено",
    "On-Hold": "Отложено",
    "Dropped": "Брошено",
    "Plan to Watch": "Запланировано",
    "Not in list": "Нет в списке",
    "All": "Все",
    "No score": "Без оценки",
    "Masterpiece": "Шедевр",
    "Great": "Отлично",
    "Very Good": "Очень хорошо",
    "Good": "Хорошо",
    "Fine": "Неплохо",
    "Average": "Средне",
    "Bad": "Плохо",
    "Very Bad": "Очень плохо",
    "Horrible": "Ужасно",
    "Appalling": "Отвратительно",
    "{progress} ep.": "{progress} эп.",
    "Rate": "Оценить",
    "MAL status": "Статус MAL",
    "Watched episodes": "Просмотрено серий",
    "MAL link": "Привязка MAL",
    "Remove from MAL": "Удалить из MAL",
    "Link": "Привязать",
    "Remove": "Удалить",
    "MyAnimeList: searching…": "MyAnimeList: поиск…",
    "MyAnimeList: error": "MyAnimeList: ошибка",
    "Not linked to MyAnimeList: press l to choose": "Не привязано к MyAnimeList: нажмите l, чтобы выбрать",
    "MyAnimeList updated: {summary}": "MyAnimeList обновлён: {summary}",
    "«{title}» removed from your MyAnimeList": "«{title}» удалено из вашего MyAnimeList",
    "Remove «{title}» from your MyAnimeList? The score and progress will be lost.": (
        "Удалить «{title}» из вашего MyAnimeList? Оценка и прогресс будут потеряны."
    ),
    "Mark «{title}» as watched on MyAnimeList?": "Отметить «{title}» просмотренной на MyAnimeList?",
    "The episode is not counted on MyAnimeList: link the title first (l on the title page)": (
        "Серия не засчитана на MyAnimeList: сначала привяжите тайтл (l на странице тайтла)"
    ),
    "{error}. Log in again in settings (F2)": "{error}. Войдите заново в настройках (F2)",
    "You have watched «{title}» to the end. Rate it on MyAnimeList?": (
        "Вы досмотрели «{title}» до конца. Поставить оценку на MyAnimeList?"
    ),
    "Esc — later": "Esc — позже",
    "Log in to MyAnimeList in settings (F2)": "Войдите в MyAnimeList в настройках (F2)",
    "No titles with this status": "Нет тайтлов с этим статусом",
    "Pick «{title}» in the search results: it will be linked to MyAnimeList": (
        "Выберите «{title}» в результатах поиска: тайтл привяжется к MyAnimeList"
    ),
    "Rate «{title}»": "Оценка «{title}»",
    "Digits 1-9, 0 for 10": "Цифры 1-9, 0 — это 10",
    "Watched episodes of «{title}»": "Просмотрено серий «{title}»",
    "of {total}": "из {total}",
    "of {total}, Enter to save": "из {total}, Enter — сохранить",
    "Find the title on MyAnimeList": "Найти тайтл на MyAnimeList",
    "Original or english title, Enter to search": "Оригинальное или английское название, Enter — искать",
    "At least 3 characters": "Минимум 3 символа",
    "Log in to MyAnimeList": "Вход в MyAnimeList",
    "Or paste the address of the page you were redirected to": (
        "Или вставьте адрес страницы, на которую вас перенаправило"
    ),
    "Open browser": "Открыть браузер",
    "Copy link": "Скопировать ссылку",
    "Allow access in the browser, it will return you here automatically.": (
        "Разрешите доступ в браузере, после этого вы автоматически вернётесь сюда."
    ),
    "Open this link in a browser and allow access:": "Откройте ссылку в браузере и разрешите доступ:",
    "Port of {url} is busy: paste the redirect address manually": (
        "Порт {url} занят: вставьте адрес перенаправления вручную"
    ),
    "Could not open the browser, copy the link": "Не удалось открыть браузер, скопируйте ссылку",
    "Link copied": "Ссылка скопирована",
    "Create an API client at {url}: App Type «other», App Redirect URL {redirect}. Paste its Client ID here.": (
        "Создайте API-клиент на {url}: App Type «other», App Redirect URL {redirect}. Вставьте его Client ID сюда."
    ),
    "Count watched episodes and update the status": "Засчитывать просмотренные серии и менять статус",
    "Logged in as {name}": "Вход выполнен: {name}",
    "Log out": "Выйти",
    "Not logged in": "Вход не выполнен",
    "Log in": "Войти",
    "Enter the Client ID first": "Сначала укажите Client ID",
    "Show posters on the title page": "Показывать постеры на странице тайтла",
    "Poster rendering": "Отрисовка постеров",
    "Auto (no sixel)": "Авто (без sixel)",
    "Graphics (kitty protocol / sixel)": "Графика (kitty-протокол / sixel)",
    "Colored blocks": "Цветные блоки",
    "Subtitles from other sites": "Субтитры с других сайтов",
    "SUB": "СУБ",
    "loading subtitles from other sites…": "загрузка субтитров с других сайтов…",
    "no subtitles: t — search other sites": "нет субтитров: t — искать на других сайтах",
    "Searching «{title}» on other sites…": "Ищу «{title}» на других сайтах…",
    "Found on: {sites}": "Найдено: {sites}",
    "Other sites": "Другие сайты",
    "«{title}» on other sites": "«{title}» на других сайтах",
    "Enter — open the title on this site": "Enter — открыть тайтл на этом сайте",
    "episodes: {count}": "серий: {count}",
    "episode {num}: {players}": "серия {num}: {players}",
    "Searching: {answered} of {total} sites answered": "Поиск: ответили {answered} из {total} сайтов",
    "Found on {found} of {total} sites": "Найдено на {found} из {total} сайтов",
    "searching…": "ищу…",
    "not found": "не найдено",
    "similar name, check the season": "похожее название, проверьте сезон",
    "The title is not found on other sites": "Тайтл не найден на других сайтах",
    "Choose an episode: subtitles from these sites are added to the list": (
        "Выберите серию: субтитры с этих сайтов добавятся в список"
    ),
    "Subtitles first in the dub list": "Субтитры первыми в списке озвучек",
    "Airing": "Выходят",
    "Announced": "Анонсы",
    "ANNOUNCED": "АНОНС",
    "from {date}": "с {date}",
    "Find": "Найти",
    "Clear": "Очистить",
    "Find in the list (f): any title, english or russian": (
        "Поиск по списку (f): любое название, английское или русское"
    ),
    "Random": "Случайное",
    "Nothing matches «{query}»": "Ничего не найдено по «{query}»",
    "{user} · {shown} of {total}": "{user} · {shown} из {total}",
    "With new episodes: {new} of {total}": "С новыми сериями: {new} из {total}",
    "Nothing in Watching on MyAnimeList": "В списке «Смотрю» на MyAnimeList пусто",
    "new: {count}": "новых: {count}",
    "checking the site…": "проверяю сайт…",
    "caught up": "всё просмотрено",
    "the site did not answer": "сайт не ответил",
    "not watched in anihub yet: Enter to find it on the site": "ещё не смотрели в anihub: Enter — найти на сайте",
    "NEW": "НОВОЕ",
    "new episode {ago}": "новая серия {ago}",
    "{count} min ago": "{count} мин назад",
    "{count} h ago": "{count} ч назад",
    "{count} d ago": "{count} дн назад",
    "premiered {date}, no episodes on the site yet": "премьера {date}, серий на сайте пока нет",
    "Airing: {airing} · Announced: {announced}": "Выходят: {airing} · Анонсы: {announced}",
}

UK: dict[str, str] = {
    # app
    "Settings": "Налаштування",
    "Stop": "Стоп",
    "Quit": "Вихід",
    "Playback error: {error}": "Помилка відтворення: {error}",
    "Update available": "Доступне оновлення",
    "Run `anihub update` to upgrade": "Оновіть: anihub update",
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
    "Save": "Зберегти",
    "Cancel": "Скасувати",
    "Language": "Мова",
    "Source": "Джерело",
    "Quality (or the closest available)": "Якість (або найближча доступна)",
    "Player": "Плеєр",
    "Other (command)": "Інший (команда)",
    "Player command": "Команда плеєра",
    "Extra player arguments": "Додаткові аргументи плеєра",
    "Load video through anihub (fast start, reconnects)": (
        "Завантажувати відео через anihub (швидкий старт, перепідключення)"
    ),
    "Episodes per player playlist": "Серій в одному плейлисті плеєра",
    "Proxy": "Проксі",
    "format: scheme://user:password@host:port": "формат: scheme://user:password@host:port",
    "HTTP timeout, seconds": "Тайм-аут HTTP, секунд",
    "Fix the highlighted fields": "Виправте виділені поля",
    "Enter the player command": "Вкажіть команду плеєра",
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
    "Poster rendering": "Відображення постерів",
    "Auto (no sixel)": "Авто (без sixel)",
    "Graphics (kitty protocol / sixel)": "Графіка (kitty-протокол / sixel)",
    "Colored blocks": "Кольорові блоки",
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
}

CATALOGS: dict[str, dict[str, str]] = {"en": {}, "ru": RU, "uk": UK}

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
