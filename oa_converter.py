from __future__ import annotations

import csv
import json
import re
import threading
import time
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

try:
    from rapidfuzz import fuzz

    _HAS_RAPIDFUZZ = True
except ImportError:
    from difflib import SequenceMatcher

    _HAS_RAPIDFUZZ = False


# -----------------------------------------------------------------------------
# Constants / defaults
# -----------------------------------------------------------------------------

ANILIST_URL = "https://graphql.anilist.co"

# Minimalne zapytanie bez parametru type, bo AniList potrafi zrzucać 400
# na enum MediaType przy pewnych konfiguracjach / klientach.
# Typ i tak weryfikujemy lokalnie po pól `format`.
ANILIST_SEARCH_QUERY = """
query ($search: String, $sort: [MediaSort]) {
  Page(perPage: 10) {
    media(search: $search, sort: $sort) {
      id
      idMal
      title {
        romaji
        english
        native
      }
      format
      episodes
      status
    }
  }
}
"""

# ~37 zapytań/min. AniList dławi się przy burstach (~85/min kończyło się 429
# co ~30 tytułów, bo jeden wpis potrafi wygenerować 2-3 zapytania wariantów).
# Wolniej, ale za to CIĄGLE i bez przestojów.
# Empiria z jazdy live: ~41 zapytań/min kończyło się 429 (Retry-After ~14 s),
# a ~25/min jechało stabilnie. Startujemy więc poniżej obserwowanego limitu:
# wolniej, ale CIĄGLE - bez stop-and-go.
DEFAULT_REQUEST_DELAY = 2.20
DEFAULT_TIMEOUT = 20
DEFAULT_MAX_RETRIES = 6

# Adaptacyjny throttling (AIMD): po 429/5xx zwalniamy, po długiej serii sukcesów
# delikatnie przyspieszamy (ale nie wyżej niż podłoga min_delay).
DEFAULT_MAX_REQUESTS_PER_MINUTE = 32.0   # twarde, suwakowe okno 60 s
DEFAULT_MIN_DELAY = 2.00                 # podłoga opóźnienia między zapytaniami
DEFAULT_MAX_DELAY = 8.00                 # sufit opóźnienia po kolejnych 429
DEFAULT_CACHE_FILE = "anilist_search_cache.json"
DEFAULT_AUTO_THRESHOLD = 92.0
DEFAULT_REVIEW_THRESHOLD = 75.0
MAX_CANDIDATES_PER_ENTRY = 10

STATUS_MAP = {
    "Oglądam": "Watching",
    "Obejrzane": "Completed",
    "Planuje": "Plan to Watch",
    "Wstrzymane": "On-Hold",
    "Porzucone": "Dropped",
    "Nie oglądam": "On-Hold",
}

TYPE_ALIASES = {
    "tv": "TV",
    "tv short": "TV_SHORT",
    "tvshort": "TV_SHORT",
    "movie": "MOVIE",
    "special": "SPECIAL",
    "ona": "ONA",
    "ova": "OVA",
    "music": "MUSIC",
}


# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------


class AniListRequestError(RuntimeError):
    """Błąd zapytania do AniList GraphQL."""


class AniListCancelled(RuntimeError):
    """Anulowanie w trakcie pauzy rate-limitu / retry (sygnał z GUI)."""


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def strip_accents(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in value if not unicodedata.combining(ch))


def normalize_title(value: str) -> str:
    value = strip_accents(value or "").lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def parse_rating(value: str | None) -> float | None:
    value = (value or "").strip()
    if not value or value == "-":
        return None

    try:
        rating = float(value)
    except ValueError:
        return None

    if rating <= 0:
        return None

    return rating


def parse_progress(value: str | None) -> tuple[int, int]:
    value = (value or "").strip()

    if not value:
        return 0, 0

    if "/" not in value:
        try:
            watched = int(value)
        except ValueError:
            return 0, 0
        return max(watched, 0), 0

    left, right = value.split("/", 1)

    try:
        watched = int(left.strip())
        total = int(right.strip())
    except ValueError:
        return 0, 0

    watched = max(watched, 0)
    total = max(total, 0)

    if total and watched > total:
        watched = total

    if total == 0 and watched > 0:
        total = watched

    return watched, total


def _xml_escape(value: Any) -> str:
    text = str(value)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def _similarity(a: str, b: str) -> float:
    a = normalize_title(a)
    b = normalize_title(b)

    if not a or not b:
        return 0.0

    if a == b:
        return 100.0

    if _HAS_RAPIDFUZZ:
        wratio = fuzz.WRatio(a, b)
        token_set = fuzz.token_set_ratio(a, b)
        return 0.55 * wratio + 0.45 * token_set

    # Fallback bez rapidfuzz.
    seq = SequenceMatcher(None, a, b).ratio() * 100.0

    tokens_a = set(a.split())
    tokens_b = set(b.split())

    if tokens_a and tokens_b:
        jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b) * 100.0
    else:
        jaccard = 0.0

    return 0.70 * seq + 0.30 * jaccard


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------


@dataclass
class Entry:
    """
    Pojedynczy wpis z CSV z ogladajanime.pl.
    """

    local_id: int
    title: str
    status_pl: str
    rating: float | None
    watched: int
    total: int
    type: str

    normalized_title: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.normalized_title = normalize_title(self.title)


@dataclass
class Suggestion:
    """
    Propozycja dopasowania z AniList dla konkretnego wpisu CSV.

    GUI powinno pokazywać użytkownikowi:
    - entry_title,
    - title_romaji / title_english / title_native,
    - format,
    - episodes,
    - mal_id,
    - confidence.
    """

    entry_local_id: int
    entry_title: str

    anilist_id: int
    mal_id: int | None

    title_romaji: str
    title_english: str | None = None
    title_native: str | None = None

    format: str | None = None
    episodes: int | None = None
    status: str | None = None

    confidence: float = 0.0
    rank: int = 0


@dataclass
class ConfirmedMatch:
    """
    Pewne dopasowanie CSV -> MAL ID.

    Tylko takie wpisy trafiają do XML.
    """

    entry: Entry
    mal_id: int

    anilist_id: int | None = None
    title_romaji: str | None = None

    confidence: float = 0.0
    source: str = "auto"  # auto / manual

    total_episodes: int | None = None


@dataclass
class LoadResult:
    count: int
    entries: list[Entry]
    errors: list[str] = field(default_factory=list)


@dataclass
class SearchStats:
    total: int = 0
    confirmed_auto: int = 0
    uncertain: int = 0
    low_confidence: int = 0
    no_candidates: int = 0
    errors: int = 0


@dataclass
class SearchResult:
    """
    Wynik wyszukiwania w AniList.

    GUI zwykle robi tak:
    1. Pokazuje result.stats.
    2. Pokazuje result.confirmed jako automatycznie dopasowane.
    3. Pokazuje result.uncertain do ręcznej weryfikacji.
    """

    confirmed: list[ConfirmedMatch]

    # Zgodnie z Twoją prośbą: klucz = tytuł z ogladajanime.pl.
    uncertain: dict[str, list[Suggestion]]

    # Bezpieczniejsze dla GUI, gdyby były duplikaty tytułów.
    uncertain_by_entry_id: dict[int, list[Suggestion]]

    uncertain_entries: list[Entry]
    stats: SearchStats
    errors: list[str] = field(default_factory=list)


@dataclass
class XmlResult:
    content: str
    count: int
    skipped_missing_mal_id: int = 0
    path: Path | None = None


# -----------------------------------------------------------------------------
# AniList client
# -----------------------------------------------------------------------------


class AniListClient:
    """
    Klient GraphQL do AniList z CIĄGŁYM (nie burstowym) rate limitem:

    - suwakowe okno `max_requests_per_minute` (domyślnie 35 / 60 s) - wygładza
      ruch nawet wtedy, gdy jeden wpis CSV generuje 2-3 zapytania wariantów,
    - minimalna przerwa `current_delay` między zapytaniami (start: `request_delay`),
    - AIMD: każde 429/5xx podnosi `current_delay` (*1.5 + 0.2 s, sufit `max_delay`)
      i pauzuje co najmniej `Retry-After`;
      20 sukcesów z rzędu delikatnie zwraca szybkość (*0.90, podłoga `min_delay`),
    - retry z backoffem dla błędów sieciowych,
    - pauzy są chunkowane (co 0.25 s), więc `cancel_event` zatrzymuje klienta
      także w trakcie długiego czekania na Retry-After,
    - cache na dysku + lock do pracy z wątków.

    Hook dla GUI:
        client.on_rate_event = lambda kind, info: ...
    kind: "backoff" | "window_wait" | "speedup" | "network_retry"
    """

    def __init__(
        self,
        request_delay: float = DEFAULT_REQUEST_DELAY,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        cache_path: str | Path | None = DEFAULT_CACHE_FILE,
        user_agent: str = "oa-anilist-converter/1.0",
        max_requests_per_minute: float = DEFAULT_MAX_REQUESTS_PER_MINUTE,
        min_delay: float = DEFAULT_MIN_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        url: str = ANILIST_URL,
    ) -> None:
        self.base_delay = max(0.05, float(request_delay))
        self.current_delay = self.base_delay
        self.min_delay = max(0.05, min(float(min_delay), self.base_delay))
        self.max_delay = max(self.base_delay, float(max_delay))
        self.max_rpm = max(1.0, float(max_requests_per_minute))
        self.url = url
        self.timeout = int(timeout)
        self.max_retries = max(1, int(max_retries))

        self.cache_path = Path(cache_path) if cache_path else None

        self._lock = threading.Lock()
        self._last_request_time = 0.0
        self._timestamps: deque[float] = deque()
        self._ok_streak = 0

        # Ustawiane przez converter (search_all) / GUI:
        self.cancel_event: threading.Event | None = None
        self.on_rate_event: Callable[[str, dict[str, Any]], None] | None = None

        self.stats: dict[str, int] = {
            "requests": 0,
            "cache_hits": 0,
            "http_429": 0,
            "http_5xx": 0,
            "network_errors": 0,
            "window_waits": 0,
        }

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": user_agent,
            }
        )

        self._cache: dict[str, list[dict[str, Any]]] = self._load_cache()

    def rate_info(self) -> dict[str, Any]:
        """Stan tempa dla GUI (podgląd tempa i statystyk limitu)."""
        return {
            "current_delay": round(self.current_delay, 2),
            "min_delay": round(self.min_delay, 2),
            "max_delay": round(self.max_delay, 2),
            "max_rpm": self.max_rpm,
            "pace_per_min": round(60.0 / max(0.05, self.current_delay), 1),
            **self.stats,
        }

    def _emit(self, kind: str, **info: Any) -> None:
        if self.on_rate_event is None:
            return
        try:
            self.on_rate_event(kind, info)
        except Exception:
            # Hook GUI nie może wywalić klienta.
            pass

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _sleep(self, seconds: float) -> None:
        """
        Pauza odporna na anulowanie: śpi w kawałkach po 0.25 s i sprawdza
        cancel_event. Rzuca AniListCancelled, jeśli użytkownik przerwał.
        """
        if seconds <= 0:
            if self._cancelled():
                raise AniListCancelled("Anulowano podczas pauzy.")
            return

        deadline = time.monotonic() + seconds
        while True:
            if self._cancelled():
                raise AniListCancelled("Anulowano podczas pauzy.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.25, remaining))

    def _load_cache(self) -> dict[str, list[dict[str, Any]]]:
        if self.cache_path is None or not self.cache_path.exists():
            return {}

        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        return {}

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return

        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # Cache nie może wywalić całego importu.
            pass

    def _wait_for_rate_limit(self) -> None:
        """
        Wygładza ruch: najpierw twarde okno max_rpm / 60 s, potem minimalna
        przerwa current_delay. Dzięki temu analiza idzie ciągłym, równym
        strumieniem zamiast burst -> 429 -> długi przestój.
        """
        now = time.monotonic()

        while self._timestamps and now - self._timestamps[0] > 60.0:
            self._timestamps.popleft()

        if len(self._timestamps) >= int(self.max_rpm):
            wait = self._timestamps[0] + 60.05 - now
            if wait > 0:
                self.stats["window_waits"] += 1
                self._emit("window_wait", wait=round(wait, 1), max_rpm=self.max_rpm)
                self._sleep(wait)
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] > 60.0:
                    self._timestamps.popleft()

        wait = self.current_delay - (now - self._last_request_time)
        if wait > 0:
            self._sleep(wait)

        self._last_request_time = time.monotonic()
        self._timestamps.append(self._last_request_time)

    def _slow_down(self, factor: float = 1.5, bump: float = 0.2) -> float:
        before = self.current_delay
        self.current_delay = min(self.max_delay, self.current_delay * factor + bump)
        self._ok_streak = 0
        return before

    def _speed_up(self) -> None:
        self._ok_streak += 1
        if self._ok_streak >= 20 and self.current_delay > self.min_delay:
            self.current_delay = max(self.min_delay, self.current_delay * 0.90)
            self._ok_streak = 0
            self._emit("speedup", delay=round(self.current_delay, 2),
                       pace=round(60.0 / self.current_delay, 1))

    def search(self, title: str) -> list[dict[str, Any]]:
        """
        Szuka tytułu w AniList i zwraca surowe węzły `media`.

        Zwraca listę słowników z polami:
        - id
        - idMal
        - title { romaji, english, native }
        - format
        - episodes
        - status
        """

        title = (title or "").strip()
        if not title:
            return []

        cache_key = normalize_title(title)

        with self._lock:
            if cache_key in self._cache:
                self.stats["cache_hits"] += 1
                return self._cache[cache_key]

            self._wait_for_rate_limit()

            payload = {
                "query": ANILIST_SEARCH_QUERY,
                "variables": {
                    "search": title,
                    "sort": ["SEARCH_MATCH", "POPULARITY_DESC"],
                },
            }

            last_error: Exception | None = None

            for attempt in range(self.max_retries):
                if self._cancelled():
                    raise AniListCancelled("Anulowano wyszukiwanie.")

                try:
                    self.stats["requests"] += 1
                    response = self.session.post(
                        self.url,
                        json=payload,
                        timeout=self.timeout,
                    )
                except requests.RequestException as exc:
                    last_error = exc
                    self.stats["network_errors"] += 1
                    pause = min(30.0, 2 ** attempt)
                    self._emit("network_retry", attempt=attempt + 1,
                               pause=round(pause, 1))
                    self._sleep(pause)
                    self._wait_for_rate_limit()
                    continue

                if response.status_code == 200:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise AniListRequestError(
                            "AniList zwrócił nieprawidłowy JSON."
                        ) from exc

                    if body.get("errors"):
                        raise AniListRequestError(
                            "AniList GraphQL error: "
                            + json.dumps(body["errors"], ensure_ascii=False)
                        )

                    page = (body.get("data") or {}).get("Page") or {}
                    nodes = page.get("media") or []

                    self._cache[cache_key] = nodes
                    self._save_cache()
                    self._speed_up()

                    return nodes

                if response.status_code == 429 or response.status_code >= 500:
                    if response.status_code == 429:
                        self.stats["http_429"] += 1
                    else:
                        self.stats["http_5xx"] += 1

                    retry_after = response.headers.get("Retry-After")
                    try:
                        retry_after_s = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        retry_after_s = 0.0

                    before = self._slow_down()
                    pause = max(retry_after_s, self.current_delay) + 0.25

                    self._emit(
                        "backoff",
                        status=response.status_code,
                        retry_after=round(retry_after_s, 1),
                        delay_before=round(before, 2),
                        delay_after=round(self.current_delay, 2),
                        pause=round(pause, 1),
                        pace=round(60.0 / self.current_delay, 1),
                    )

                    self._sleep(pause)
                    self._wait_for_rate_limit()
                    continue

                # 400, 401, 403 itd.
                raise AniListRequestError(
                    f"HTTP {response.status_code}: {response.text[:500]}"
                )

            if last_error is not None:
                raise AniListRequestError(str(last_error)) from last_error

            raise AniListRequestError("AniList search failed after retries.")


# -----------------------------------------------------------------------------
# Main converter class
# -----------------------------------------------------------------------------


class OaToMalConverter:
    """
    Konwerter eksportu CSV z ogladajanime.pl do minimalnego MAL XML
    przy użyciu AniList jako źródła `idMal`.

    Typowy workflow:

        converter = OaToMalConverter()
        loaded = converter.load_csv("lista.csv")

        result = converter.search_all(
            progress_callback=my_progress_fn,
            cancel_event=my_cancel_event,
        )

        # GUI pokazuje:
        # - result.confirmed
        # - result.uncertain
        # - result.uncertain_by_entry_id

        # Użytkownik wybiera sugestię:
        converter.confirm_suggestion(selected_suggestion)

        # Albo odrzuca wpis:
        converter.reject_entry(entry_local_id)

        # Na koniec:
        xml = converter.generate_xml(output_path="mal_import.xml")
    """

    def __init__(
        self,
        request_delay: float = DEFAULT_REQUEST_DELAY,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        cache_path: str | Path | None = DEFAULT_CACHE_FILE,
        auto_threshold: float = DEFAULT_AUTO_THRESHOLD,
        review_threshold: float = DEFAULT_REVIEW_THRESHOLD,
        status_map: dict[str, str] | None = None,
        unknown_status: str = "Watching",
        max_requests_per_minute: float = DEFAULT_MAX_REQUESTS_PER_MINUTE,
        min_delay: float = DEFAULT_MIN_DELAY,
        max_delay: float = DEFAULT_MAX_DELAY,
        url: str = ANILIST_URL,
    ) -> None:
        self.client = AniListClient(
            request_delay=request_delay,
            timeout=timeout,
            max_retries=max_retries,
            cache_path=cache_path,
            max_requests_per_minute=max_requests_per_minute,
            min_delay=min_delay,
            max_delay=max_delay,
            url=url,
        )

        self.auto_threshold = float(auto_threshold)
        self.review_threshold = float(review_threshold)

        self.status_map = dict(status_map or STATUS_MAP)
        self.unknown_status = unknown_status

        self.entries: list[Entry] = []
        self.entry_by_id: dict[int, Entry] = {}

        self.confirmed_matches: list[ConfirmedMatch] = []

        self.uncertain_by_title: dict[str, list[Suggestion]] = {}
        self.uncertain_by_entry_id: dict[int, list[Suggestion]] = {}
        self.uncertain_entries: list[Entry] = []

        self.search_errors: list[str] = []

    # -------------------------------------------------------------------------
    # CSV loading
    # -------------------------------------------------------------------------

    def load_csv(self, path: str | Path) -> LoadResult:
        """
        Wczytuje CSV z ogladajanime.pl.

        Oczekiwane kolumny:
        - "#"
        - "Okładka"  # ignorowane
        - "Tytuł"
        - "Status"
        - "Ocena"
        - "Postęp"
        - "Typ"

        Zwraca LoadResult z:
        - count,
        - entries,
        - errors.

        Uwaga:
        Wywołanie tej metody czyści wcześniejsze wyniki wyszukiwania i ręczne
        potwierdzenia.
        """

        path = Path(path)

        if not path.exists():
            raise FileNotFoundError(f"Nie znaleziono pliku CSV: {path}")

        entries: list[Entry] = []
        errors: list[str] = []

        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)

            for row_number, row in enumerate(reader, start=2):  # 1 = header
                try:
                    raw_id = (row.get("#") or "").strip()
                    if not raw_id:
                        continue

                    local_id = int(raw_id)

                    title = (row.get("Tytuł") or "").strip()
                    if not title:
                        errors.append(f"Wiersz {row_number}: pusty tytuł.")
                        continue

                    status_pl = (row.get("Status") or "").strip()
                    rating = parse_rating(row.get("Ocena"))
                    watched, total = parse_progress(row.get("Postęp"))
                    anime_type = (row.get("Typ") or "").strip()

                    entries.append(
                        Entry(
                            local_id=local_id,
                            title=title,
                            status_pl=status_pl,
                            rating=rating,
                            watched=watched,
                            total=total,
                            type=anime_type,
                        )
                    )

                except Exception as exc:
                    errors.append(f"Wiersz {row_number}: {exc}")

        self.entries = entries
        self.entry_by_id = {entry.local_id: entry for entry in entries}

        self.clear_results()

        return LoadResult(
            count=len(entries),
            entries=entries,
            errors=errors,
        )

    def clear_results(self) -> None:
        """
        Czyści wyniki wyszukiwania, potwierdzenia i błędy.
        Nie czyści wczytanych entries.
        """

        self.confirmed_matches.clear()

        self.uncertain_by_title.clear()
        self.uncertain_by_entry_id.clear()
        self.uncertain_entries.clear()

        self.search_errors.clear()

    # -------------------------------------------------------------------------
    # AniList matching
    # -------------------------------------------------------------------------

    def search_all(
        self,
        progress_callback: Callable[[int, int, Entry], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> SearchResult:
        """
        Wyszukuje wszystkie wczytane wpisy w AniList.

        Args:
            progress_callback:
                Funkcja wywoływana przed przetworzeniem każdego wpisu.
                Signature: (current_index, total_count, entry) -> None.

                Ważne dla GUI:
                callback może być wywoływany z wątku roboczego, więc GUI
                powinno samodzielnie przekazać aktualizacje do wątku UI.

            cancel_event:
                Opcjonalny threading.Event. Jeśli zostanie ustawiony,
                przetwarzanie zatrzyma się po bieżącym wpisie.

        Returns:
            SearchResult z:
            - confirmed: automatycznie dopasowane wpisy,
            - uncertain: dict tytuł -> sugestie,
            - uncertain_by_entry_id: dict local_id -> sugestie,
            - uncertain_entries,
            - stats,
            - errors.

        Uwaga:
            Wywołanie tej metody czyści wcześniejsze confirmed/uncertain.
        """

        self.clear_results()

        # Klient zna cancel_event, więc anulowanie działa także W TRAKCIE
        # długich pauz (Retry-After / okno rate-limitu).
        previous_cancel = self.client.cancel_event
        self.client.cancel_event = cancel_event

        entries = self.entries
        stats = SearchStats(total=len(entries))

        try:
            for index, entry in enumerate(entries, start=1):
                if cancel_event is not None and cancel_event.is_set():
                    break

                if progress_callback is not None:
                    try:
                        progress_callback(index, len(entries), entry)
                    except Exception:
                        # Callback z GUI nie może wywalić background workera.
                        pass

                try:
                    candidates = self._search_candidates(entry)

                    scored: list[tuple[float, Suggestion]] = []

                    for candidate in candidates:
                        confidence = self._score_candidate(entry, candidate)
                        scored.append((confidence, candidate))

                    scored.sort(key=lambda item: item[0], reverse=True)

                    top_suggestions = [
                        suggestion for _, suggestion in scored[:MAX_CANDIDATES_PER_ENTRY]
                    ]

                    for rank, suggestion in enumerate(top_suggestions, start=1):
                        suggestion.rank = rank

                    best_confidence = scored[0][0] if scored else 0.0
                    best_suggestion = scored[0][1] if scored else None

                    if (
                        best_suggestion is not None
                        and best_suggestion.mal_id is not None
                        and best_confidence >= self.auto_threshold
                    ):
                        match = ConfirmedMatch(
                            entry=entry,
                            mal_id=best_suggestion.mal_id,
                            anilist_id=best_suggestion.anilist_id,
                            title_romaji=best_suggestion.title_romaji,
                            confidence=round(best_confidence, 2),
                            source="auto",
                            total_episodes=(
                                best_suggestion.episodes or entry.total or None
                            ),
                        )

                        self.confirmed_matches.append(match)
                        stats.confirmed_auto += 1

                    else:
                        self.uncertain_entries.append(entry)
                        self.uncertain_by_entry_id[entry.local_id] = top_suggestions
                        self.uncertain_by_title.setdefault(entry.title, []).extend(
                            top_suggestions
                        )

                        stats.uncertain += 1

                        if best_confidence < self.review_threshold:
                            stats.low_confidence += 1

                        if not top_suggestions:
                            stats.no_candidates += 1

                except AniListCancelled:
                    # Anulowanie złapane w środku wpisu (np. podczas pauzy 429).
                    break

                except Exception as exc:
                    self.search_errors.append(f"{entry.local_id} | {entry.title}: {exc}")

                    self.uncertain_entries.append(entry)
                    self.uncertain_by_entry_id.setdefault(entry.local_id, [])
                    self.uncertain_by_title.setdefault(entry.title, [])

                    stats.errors += 1
                    stats.uncertain += 1
                    stats.low_confidence += 1
                    stats.no_candidates += 1


        finally:
            self.client.cancel_event = previous_cancel
        return SearchResult(
            confirmed=self.confirmed_matches,
            uncertain=self.uncertain_by_title,
            uncertain_by_entry_id=self.uncertain_by_entry_id,
            uncertain_entries=self.uncertain_entries,
            stats=stats,
            errors=self.search_errors,
        )

    def _search_candidates(self, entry: Entry) -> list[Suggestion]:
        """
        Szuka kandydatów dla jednego wpisu.

        Najpierw próbuje oryginalny tytuł.
        Jeśli brak wyników, próbuje warianty bez dopisków typu "(TV)"
        oraz bez końcówek "2nd Season", "Part 2" itd.
        """

        variants = self._title_variants(entry.title)

        candidates: list[Suggestion] = []
        seen_anilist_ids: set[int] = set()

        for variant in variants:
            nodes = self.client.search(variant)

            for node in nodes:
                suggestion = self._suggestion_from_node(entry, node)

                if suggestion is None:
                    continue

                if suggestion.anilist_id in seen_anilist_ids:
                    continue

                seen_anilist_ids.add(suggestion.anilist_id)
                candidates.append(suggestion)

            if candidates:
                break

        return candidates

    def _title_variants(self, title: str) -> list[str]:
        variants = [title]

        cleaned = re.sub(
            r"\s*\((?:TV|ONA|OVA|Special|Movie|Music|TV Short)\)\s*",
            " ",
            title,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        cleaned2 = re.sub(
            r"\s+(?:\d+(?:st|nd|rd|th)\s+Season|Part\s+\d+)\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

        for variant in (cleaned, cleaned2):
            if variant and variant not in variants:
                variants.append(variant)

        return variants

    def _suggestion_from_node(
        self,
        entry: Entry,
        node: dict[str, Any],
    ) -> Suggestion | None:
        anilist_id = node.get("id")

        if anilist_id is None:
            return None

        titles = node.get("title") or {}

        return Suggestion(
            entry_local_id=entry.local_id,
            entry_title=entry.title,
            anilist_id=int(anilist_id),
            mal_id=node.get("idMal"),
            title_romaji=titles.get("romaji") or "",
            title_english=titles.get("english"),
            title_native=titles.get("native"),
            format=node.get("format"),
            episodes=node.get("episodes"),
            status=node.get("status"),
        )

    def _score_candidate(self, entry: Entry, suggestion: Suggestion) -> float:
        """
        Wystawia confidence 0-100.

        Czynniki:
        - podobieństwo tytułu,
        - zgodność liczby odcinków,
        - zgodność typu,
        - obecność MAL ID.
        """

        candidate_titles = [
            suggestion.title_romaji,
            suggestion.title_english,
            suggestion.title_native,
        ]

        base_scores = [
            _similarity(entry.normalized_title, title)
            for title in candidate_titles
            if title
        ]

        score = max(base_scores) if base_scores else 0.0

        # Bonus/penalizacja za liczbę odcinków.
        if entry.total > 0 and suggestion.episodes and suggestion.episodes > 0:
            diff = abs(entry.total - suggestion.episodes)

            if diff == 0:
                score += 8.0
            elif diff <= 2:
                score += 4.0
            else:
                score -= min(25.0, float(diff * 2))

        # Bonus/penalizacja za typ.
        entry_format = TYPE_ALIASES.get((entry.type or "").strip().lower())
        candidate_format = (
            suggestion.format.strip().upper() if suggestion.format else None
        )

        if entry_format and candidate_format:
            if entry_format == candidate_format:
                score += 7.0
            else:
                score -= 6.0

        # Bez MAL ID nie da się wygenerować XML.
        if suggestion.mal_id:
            score += 5.0
        else:
            score -= 30.0

        return max(0.0, min(100.0, round(score, 2)))

    # -------------------------------------------------------------------------
    # Manual verification API for GUI
    # -------------------------------------------------------------------------

    def confirm_suggestion(self, suggestion: Suggestion) -> ConfirmedMatch:
        """
        Potwierdza ręcznie wybraną sugestię z AniList.

        Używane przez GUI, gdy użytkownik wybierze jedną z propozycji
        dla wpisu niepewnego.

        Raises:
            ValueError: jeśli sugestia nie ma MAL ID.
            KeyError: jeśli nie znaleziono powiązanego wpisu CSV.
        """

        if suggestion.mal_id is None:
            raise ValueError("Sugestia nie posiada MAL ID.")

        entry = self.entry_by_id.get(suggestion.entry_local_id)

        if entry is None:
            raise KeyError(
                f"Nie znaleziono wpisu CSV o local_id={suggestion.entry_local_id}."
            )

        match = ConfirmedMatch(
            entry=entry,
            mal_id=suggestion.mal_id,
            anilist_id=suggestion.anilist_id,
            title_romaji=suggestion.title_romaji,
            confidence=suggestion.confidence,
            source="manual",
            total_episodes=suggestion.episodes or entry.total or None,
        )

        self._upsert_confirmed_match(match)
        self._remove_uncertain_entry(entry.local_id)

        return match

    def confirm_manual_mal_id(
        self,
        entry_local_id: int,
        mal_id: int,
        *,
        anilist_id: int | None = None,
        title_romaji: str | None = None,
        total_episodes: int | None = None,
        confidence: float = 100.0,
    ) -> ConfirmedMatch:
        """
        Potwierdza wpis ręcznie podanym MAL ID.

        Przydatne, jeśli GUI ma pole "wpisz MAL ID ręcznie"
        albo jeśli AniList nie zwrócił `idMal`.
        """

        entry = self.entry_by_id.get(entry_local_id)

        if entry is None:
            raise KeyError(f"Nie znaleziono wpisu CSV o local_id={entry_local_id}.")

        match = ConfirmedMatch(
            entry=entry,
            mal_id=int(mal_id),
            anilist_id=anilist_id,
            title_romaji=title_romaji,
            confidence=float(confidence),
            source="manual",
            total_episodes=total_episodes or entry.total or None,
        )

        self._upsert_confirmed_match(match)
        self._remove_uncertain_entry(entry_local_id)

        return match

    def reject_entry(self, entry_local_id: int) -> bool:
        """
        Odrzuca wpis z listy niepewnych.

        Nie dodaje go do confirmed.
        Zwraca True, jeśli wpis był na liście niepewnych i został usunięty.
        """

        before = len(self.uncertain_entries)
        self._remove_uncertain_entry(entry_local_id)
        return len(self.uncertain_entries) < before

    def _upsert_confirmed_match(self, match: ConfirmedMatch) -> None:
        """
        Zapobiega duplikatom dla tego samego wpisu CSV.
        Jeśli wpis był już potwierdzony, nadpisuje stare potwierdzenie.
        """

        self.confirmed_matches = [
            existing
            for existing in self.confirmed_matches
            if existing.entry.local_id != match.entry.local_id
        ]

        self.confirmed_matches.append(match)

    def _remove_uncertain_entry(self, entry_local_id: int) -> None:
        self.uncertain_entries = [
            entry
            for entry in self.uncertain_entries
            if entry.local_id != entry_local_id
        ]

        self.uncertain_by_entry_id.pop(entry_local_id, None)

        for title, suggestions in list(self.uncertain_by_title.items()):
            filtered = [
                suggestion
                for suggestion in suggestions
                if suggestion.entry_local_id != entry_local_id
            ]

            if filtered:
                self.uncertain_by_title[title] = filtered
            else:
                self.uncertain_by_title.pop(title, None)

    # -------------------------------------------------------------------------
    # XML generation
    # -------------------------------------------------------------------------

    def generate_xml(
        self,
        matches: Iterable[ConfirmedMatch] | None = None,
        output_path: str | Path | None = None,
    ) -> XmlResult:
        """
        Generuje minimalny MAL XML działający dla importu do AniList.

        Używa wyłącznie pól, które przetestowałeś jako wystarczające:

        <anime>
            <series_animedb_id>...</series_animedb_id>
            <my_watched_episodes>...</my_watched_episodes>
            <my_score>...</my_score>        # opcjonalnie
            <my_status>...</my_status>
            <update_on_import>1</update_on_import>
        </anime>

        Args:
            matches:
                Lista potwierdzonych dopasowań.
                Domyślnie używa self.confirmed_matches.

            output_path:
                Opcjonalna ścieżka do zapisu pliku XML.

        Returns:
            XmlResult z:
            - content,
            - count,
            - skipped_missing_mal_id,
            - path.
        """

        if matches is None:
            matches = self.confirmed_matches

        lines: list[str] = [
            '<?xml version="1.0" encoding="UTF-8" ?>',
            "<!-- Created by OaToMalConverter -->",
            "<myanimelist>",
            "\t<myinfo>",
            "\t\t<user_export_type>1</user_export_type>",
            "\t</myinfo>",
        ]

        count = 0
        skipped_missing_mal_id = 0

        for match in matches:
            if not match.mal_id:
                skipped_missing_mal_id += 1
                continue

            status = self._map_status(match.entry.status_pl)

            watched = max(0, int(match.entry.watched))

            if match.total_episodes and match.total_episodes > 0:
                if watched > match.total_episodes:
                    watched = match.total_episodes

            score = self._mal_score(match.entry.rating)

            lines.append("\t<anime>")
            lines.append(
                f"\t\t<series_animedb_id>{int(match.mal_id)}</series_animedb_id>"
            )
            lines.append(f"\t\t<my_watched_episodes>{watched}</my_watched_episodes>")

            if score is not None:
                lines.append(f"\t\t<my_score>{score}</my_score>")

            lines.append(f"\t\t<my_status>{_xml_escape(status)}</my_status>")
            lines.append("\t\t<update_on_import>1</update_on_import>")
            lines.append("\t</anime>")

            count += 1

        lines.append("</myanimelist>")

        content = "\n".join(lines)

        path = Path(output_path) if output_path else None

        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        return XmlResult(
            content=content,
            count=count,
            skipped_missing_mal_id=skipped_missing_mal_id,
            path=path,
        )

    def _map_status(self, status_pl: str) -> str:
        return self.status_map.get((status_pl or "").strip(), self.unknown_status)

    @staticmethod
    def _mal_score(rating: float | None) -> int | None:
        """
        MAL XML oczekuje zwykle integer 1-10.

        Jeśli brak oceny -> None -> nie dodajemy <my_score>.
        """

        if rating is None:
            return None

        if rating <= 0:
            return None

        score = int(round(rating))

        if score < 1:
            return None

        return max(1, min(10, score))
