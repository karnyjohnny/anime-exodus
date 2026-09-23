# OA → MAL Converter — GUI (DearPyGui 2.x) + klasa konwertera

Dwa pliki deliverable:

* **`oa_gui.py`** — ciemne, pełnoekranowe GUI (DearPyGui **2.3.1**),
* **`oa_converter.py`** — klasa `OaToMalConverter` (z Twojego `.txt`) z przebudowanym
  klientem AniList: **ciągła analiza bez stop-and-go** (adaptacyjny rate-limit).

Konwertuje eksport CSV z **ogladajanime.pl** do minimalnego XML-a w formacie
**MyAnimeList**, który importuje się do MAL-a i do AniList.

Testowane na dokładnie tych wersjach, które masz u siebie:
`dearpygui 2.3.1`, `RapidFuzz 3.14.6`, `requests 2.34.2`, Python 3.11+.

---

## Uruchomienie

Oba pliki wrzuć do jednego katalogu i odpal:

```bash
pip install dearpygui requests rapidfuzz
python oa_gui.py
```

Viewport startuje zmaksymalizowany; układ jest responsywny (przelicza się przy
zmianie rozmiaru okna).

---

## Wybór pliku CSV — pod osoby mniej obeznane z komputerem

Sekcja 1 to jeden duży przycisk **„Wybierz plik CSV…”**, który otwiera natywny
**File & Directory Selector** z DearPyGui (`dpg.file_dialog`, modal, dark theme):

* katalog startowy wybierany mądrze: ostatnio użyty katalog → Pulpit/Desktop →
  Pobrane/Downloads → katalog bieżący,
* rozszerzenia `.csv` podświetlone na zielono,
* po wskazaniu pliku wczytanie dzieje się **automatycznie** (bez drugiego klikania),
  a pod przyciskiem widać wybrany plik i statystyki („Wczytano N wpisów…"),
* „Wczytaj ponownie" powtarza wczytanie tego samego pliku,
* dla power-userów: zwijane „Zaawansowane: wpisz ścieżkę ręcznie".

Zły plik (brak, złe rozszerzenie, brak kolumny `Tytuł`, 0 wpisów) → czerwony
komunikat + modal z wyjaśnieniem + lista błędów parsowania w zwijanym panelu.

---

## Rate-limit AniList: ciągła analiza zamiast burst → 429 → przestój

Problem z wersji 0.70 s/zapytanie (~85/min): 429 co ~30 tytułów, bo jeden wpis
potrafi wygenerować 2-3 zapytania wariantów tytułu.

Nowy `AniListClient` (`oa_converter.py`) jedzie **wolniej, ale bez przerwy**:

| Mechanizm | Działanie |
|---|---|
| startowe tempo | `2.2 s` przerwy ≈ **27 zapytań/min** (poniżej obserwowanego limitu) |
| twarde okno | suwakowe **32 zapytania / 60 s** — wygładza także warianty tytułów |
| AIMD po 429/5xx | respekt `Retry-After` + trwałe zwolnienie opóźnienia `*1.5 + 0.2 s` (sufit `8 s`) |
| po 20 sukcesach | minimalne przyspieszenie `*0.90` (podłoga `2.0 s`) |
| anulowanie | pauzy chunkowane co 0.25 s → `Anuluj` działa **także w trakcie** długiego Retry-After (`AniListCancelled`) |
| cache | bez zmian: `anilist_search_cache.json`, trafienia liczone i pokazywane |

W GUI wszystko widać na żywo: pod paskiem postępu linia
`tempo AniList: ~25.4 zapytań/min • opóźnienie 2.36 s • zapytań 32 • 429: 1 • cache: 0`,
a każde zdarzenie limitu ląduje w dzienniku (amber/cyan), np.
`AniList HTTP 429: zwalniam do 2.36 s/zapytanie (~25.4/min), pauza 14.2 s - jadę dalej`.

Tempo dostroisz stałą na górze `oa_gui.py`:

```python
CONVERTER_KWARGS = {"request_delay": 1.5, "max_requests_per_minute": 40}
```

(albo bezpośrednio `OaToMalConverter(request_delay=…, max_requests_per_minute=…,
min_delay=…, max_delay=…)`.)

---

## Okna

| Okno | Zawartość |
|---|---|
| **Konwerter** (pełny ekran) | 1) wybór CSV dialogiem + walidacja + statystyki, 2) analiza z animowanym progresem, spinnerem, ETA, linią tempa i kartami statystyk + dziennik, 3) generowanie XML |
| **Weryfikacja niepewnych dopasowań** | `[nazwa z ogladajanime.pl]` + dropdown z propozycjami AniList + `Zapisz wybór`, ręczne MAL ID, `Odrzuć wpis` |
| **Instrukcja (co i dlaczego)** | pipeline, progi pewności, cache, **tempo i 429**, wątki, duplikaty tytułów, ostrzeżenia |
| **Import do MAL / AniList** | krok po kroku: `myanimelist.net/import.php` oraz `anilist.co/settings/import` (+ *Kopiuj* / *Otwórz w przeglądarce*) |

Przepływ: **Wybierz plik CSV… → Rozpocznij analizę → (Weryfikacja) → Generuj XML**.

Zachowania zgodne z dokumentacją klasy: `search_all()` tylko w wątku roboczym
(postęp przez `queue.Queue` → tick UI), `Anuluj` = `threading.Event`, ponowna
analiza pyta modalnie (czyści ręczne potwierdzenia), do XML trafiają tylko wpisy
potwierdzone, sugestia bez `mal_id` wymaga ręcznego ID albo odrzucenia, praca na
`uncertain_by_entry_id`, cache nigdy nie kasowany.

Czcionki: wbudowany ProggyClean nie ma polskich znaków, więc GUI binduje czcionkę
systemową (Segoe UI / DejaVu / Liberation / Noto, regular + bold).

---

## Testy (jak to było weryfikowane)

* `_dev/autotest.py`, `_dev/autotest2.py` — headless (Xvfb) pełny scenariusz UI na
  stubie `_dev/oa_converter_stub.py`; zrzuty klatek w `_dev/shots/`:
  zły/brak/dobry plik, dialog wyboru, progress, anulowanie, karty, weryfikacja
  (zapis / ręczne MAL ID / odrzuć), restart-warning, sugestia bez MAL ID, XML,
  file dialog, resize.
* `_dev/test_ratelimit.py` — lokalny mock AniList (429 z `Retry-After` co 8.
  zapytanie): ciągłość (max przerwa 4.25 s przy średniej 1.59 s), wzrost opóźnienia
  po 429, twarde okno zapytań/min, **cancel w 0.75 s podczas pauzy**, cache.
* Przebieg **live** przeciwko prawdziwemu `graphql.anilist.co` (8 tytułów, 0×429)
  oraz przypadkowy pełny przebieg 64 tytułów podczas testów UI — na żywym 429
  klient zwolnił do ~25/min i jechał dalej (widoczne w `_dev/shots/06_done_modal.png`
  z wcześniejszej iteracji).

```bash
xvfb-run -a -s "-screen 0 1920x1080x24" python3 _dev/autotest.py
xvfb-run -a -s "-screen 0 1920x1080x24" python3 _dev/autotest2.py
python3 _dev/test_ratelimit.py
```

Podglądy w katalogu głównym: `podglad_wybor_csv.png`, `podglad_dialog_pliku.png`,
`podglad_main.png`, `podglad_weryfikacja.png`, `podglad_start.png`.
