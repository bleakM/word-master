import json
import lzma
import os
import random
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets


DATA_FILE = "word_data.wmz"
LEGACY_DATA_FILE = "word_data.json"
DATA_MAGIC = b"WMZ2"
DEFAULT_REMAIN = 3
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SPLASH_IMAGE = os.path.join(APP_DIR, "word_master.png")
DEFAULT_ALGORITHM = "normal"
ALGORITHM_CODES = {"normal": 0, "ebbinghaus": 1, "scientific": 2}
CODE_TO_ALGORITHM = {code: name for name, code in ALGORITHM_CODES.items()}
FULL_SESSION_ALGORITHMS = ("normal", "scientific")
ALGORITHM_FALLBACK_ORDER = {
    "normal": ("ebbinghaus",),
    "scientific": ("ebbinghaus",),
    "ebbinghaus": ("scientific", "normal"),
}
RESULT_CODES = {"": 0, "correct": 1, "wrong": 2, "unknown": 3, "slash": 4}
CODE_TO_RESULT = {code: name for name, code in RESULT_CODES.items()}


def current_timestamp() -> int:
    return int(time.time())


@dataclass
class WordEntry:
    word: str
    meaning: str
    errors: int = 0

    def as_dict(self) -> dict:
        return {"word": self.word, "meaning": self.meaning, "errors": self.errors}


@dataclass
class WordBook:
    id: str
    name: str
    book_type: str
    tags: List[str]
    entries: List[WordEntry] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "book_type": self.book_type,
            "tags": self.tags,
            "entries": [e.as_dict() for e in self.entries],
        }


class DataStore:
    def __init__(self, path: str):
        self.path = path
        self.books: List[WordBook] = []
        self.global_errors: Dict[str, int] = {}
        self.global_words: Dict[str, str] = {}
        self.memory_states: Dict[str, dict] = {}
        self.study_states: Dict[str, dict] = {}
        self.window_geo: dict = {}
        self.settings: dict = {"algorithm": DEFAULT_ALGORITHM}
        self.history: List[dict] = []
        self.future: List[dict] = []
        self.load()

    def load(self) -> None:
        data: Optional[dict] = None
        should_save = False
        if os.path.exists(self.path):
            try:
                data = self._read_storage(self.path)
            except Exception:
                data = None
        if data is None:
            legacy_path = self._legacy_path()
            if os.path.exists(legacy_path):
                try:
                    data = self._read_legacy_json(legacy_path)
                except Exception:
                    data = self._blank_payload()
                should_save = True
            else:
                data = self._blank_payload()
                should_save = True
        self._apply_payload(data)
        self.history.clear()
        self.future.clear()
        init_payload = self._build_payload()
        if should_save or not os.path.exists(self.path):
            self._write_payload(init_payload, record_history=False, label="初始化")
        self._record_snapshot(init_payload, label="初始状态", clear_future=True)

    def save(self, push_history: bool = True, label: str = "更改") -> None:
        payload = self._build_payload()
        self._write_payload(payload, record_history=push_history, label=label or "更改")

    def _save_blank(self) -> None:
        self._write_payload(self._blank_payload(), record_history=False, label="初始化")

    def _build_payload(self) -> dict:
        return {
            "books": [b.as_dict() for b in self.books],
            "global_errors": self.global_errors,
            "global_words": self.global_words,
            "memory_states": self.memory_states,
            "study_states": self.study_states,
            "window": self.window_geo,
            "settings": self.settings,
        }

    def _apply_payload(self, data: dict) -> None:
        data = self._normalize_payload(data)
        self.books = [
            WordBook(
                id=b.get("id", str(uuid.uuid4())),
                name=b.get("name", "未命名"),
                book_type=b.get("book_type", "英-汉"),
                tags=b.get("tags", ["默认"]),
                entries=[WordEntry(e["word"], e["meaning"], e.get("errors", 0)) for e in b.get("entries", [])],
            )
            for b in data.get("books", [])
        ]
        self.global_errors = {str(k).lower(): int(v) for k, v in data.get("global_errors", {}).items()}
        self.global_words = {str(k).lower(): str(v) for k, v in data.get("global_words", {}).items()}
        self.memory_states = {
            str(k).lower(): self._normalize_memory_state(v) for k, v in data.get("memory_states", {}).items()
        }
        self.study_states = self._normalize_study_states(data.get("study_states", {}))
        self.window_geo = dict(data.get("window", {}))
        self.settings = {"algorithm": DEFAULT_ALGORITHM}
        self.settings.update(data.get("settings", {}))
        if self.settings.get("algorithm") not in ALGORITHM_CODES:
            self.settings["algorithm"] = DEFAULT_ALGORITHM
        for b in self.books:
            for e in b.entries:
                key = e.word.lower()
                self.global_errors.setdefault(key, e.errors)
                if e.meaning:
                    self.global_words.setdefault(key, e.meaning)
        for key in list(self.global_errors.keys()):
            self.global_words.setdefault(key, "")
        for key in list(self.global_words.keys()):
            self.global_errors.setdefault(key, 0)
        for key in list(self.memory_states.keys()):
            self.global_errors.setdefault(key, 0)
            self.global_words.setdefault(key, "")

    def _write_payload(self, payload: dict, record_history: bool, label: str = "更改") -> None:
        encoded = self._encode_payload(payload)
        with open(self.path, "wb") as f:
            f.write(encoded)
        if record_history:
            self._record_snapshot(payload, label=label, clear_future=True)

    def _record_snapshot(self, payload: dict, label: str = "更改", clear_future: bool = False) -> None:
        snapshot = {"payload": json.loads(json.dumps(payload)), "label": label or "更改"}
        if self.history and self.history[-1]["payload"] == snapshot["payload"]:
            return
        self.history.append(snapshot)
        if len(self.history) > 20:
            self.history = self.history[-20:]
        if clear_future:
            self.future.clear()

    def can_undo(self) -> bool:
        return len(self.history) > 1

    def can_redo(self) -> bool:
        return len(self.future) > 0

    def undo(self) -> Optional[str]:
        if not self.can_undo():
            return None
        current = self.history.pop()
        self.future.append(current)
        payload_entry = self.history[-1]
        self._apply_payload(payload_entry["payload"])
        self._write_payload(payload_entry["payload"], record_history=False, label=payload_entry["label"])
        return current["label"]

    def redo(self) -> Optional[str]:
        if not self.can_redo():
            return None
        entry = self.future.pop()
        self._apply_payload(entry["payload"])
        self._write_payload(entry["payload"], record_history=False, label=entry["label"])
        self._record_snapshot(entry["payload"], label=entry["label"], clear_future=False)
        return entry["label"]

    def unique_name(self, name: str, exclude_id: Optional[str] = None) -> str:
        base = name or "未命名"
        existing = [b.name for b in self.books if b.id != exclude_id]
        candidate = base
        idx = 1
        while candidate in existing:
            candidate = f"{base}{idx}"
            idx += 1
        return candidate

    def ensure_global_word(self, word: str, meaning: str, errors: int = 0) -> None:
        key = word.lower()
        self.global_errors[key] = max(errors, self.global_errors.get(key, errors))
        if meaning:
            self.global_words.setdefault(key, meaning)

    def rename_or_update_word(self, old_word: str, new_word: str, meaning: str) -> None:
        old_key = old_word.lower()
        new_key = new_word.lower()
        err_val = self.global_errors.pop(old_key, 0)
        err_val = max(err_val, self.global_errors.get(new_key, err_val))
        self.global_errors[new_key] = err_val
        memory_state = self.memory_states.pop(old_key, None)
        if memory_state:
            existing_memory = self.memory_states.get(new_key)
            if existing_memory:
                merged = self._normalize_memory_state(existing_memory)
                incoming = self._normalize_memory_state(memory_state)
                merged["stability"] = max(merged["stability"], incoming["stability"])
                merged["difficulty"] = max(merged["difficulty"], incoming["difficulty"])
                merged["due_at"] = max(merged["due_at"], incoming["due_at"])
                merged["last_reviewed_at"] = max(merged["last_reviewed_at"], incoming["last_reviewed_at"])
                merged["last_wrong_at"] = max(merged["last_wrong_at"], incoming["last_wrong_at"])
                merged["reps"] = max(merged["reps"], incoming["reps"])
                merged["lapses"] = max(merged["lapses"], incoming["lapses"])
                merged["streak"] = max(merged["streak"], incoming["streak"])
                merged["seen_count"] = max(merged["seen_count"], incoming["seen_count"])
                merged["last_result"] = incoming["last_result"] or merged["last_result"]
                self.memory_states[new_key] = merged
            else:
                self.memory_states[new_key] = self._normalize_memory_state(memory_state)
        old_meaning = self.global_words.pop(old_key, "")
        self.global_words[new_key] = meaning or self.global_words.get(new_key, old_meaning)

    def remove_global_word(self, word: str) -> None:
        key = word.lower()
        self.global_errors.pop(key, None)
        self.global_words.pop(key, None)
        self.memory_states.pop(key, None)

    def add_or_update_book(self, book: WordBook, book_id: Optional[str] = None) -> None:
        if book_id:
            for i, b in enumerate(self.books):
                if b.id == book_id:
                    self.books[i] = book
                    break
        else:
            self.books.append(book)
        for e in book.entries:
            self.ensure_global_word(e.word, e.meaning, e.errors)
        self._reconcile_global_words()
        self.save(label="编辑单词本" if book_id else "新建单词本")

    def delete_book(self, book_id: str) -> None:
        self.books = [b for b in self.books if b.id != book_id]
        self._reconcile_global_words()
        self.save(label="删除单词本")

    def delete_words(self, words: List[str], label: str = "删除单词") -> None:
        word_keys = {w.lower() for w in words if w}
        if not word_keys:
            return
        new_books: List[WordBook] = []
        for book in self.books:
            filtered_entries = [e for e in book.entries if e.word.lower() not in word_keys]
            book.entries = filtered_entries
            new_books.append(book)
        self.books = new_books
        for key in word_keys:
            self.remove_global_word(key)
        self._reconcile_global_words()
        self.save(label=label)

    def add_words_to_books(self, words: List[str], target_book_ids: List[str]) -> int:
        lookup = {k.lower(): v for k, v in self.global_words.items()}
        errors = {k.lower(): v for k, v in self.global_errors.items()}
        added = 0
        selected = {bid for bid in target_book_ids}
        for book in self.books:
            if book.id not in selected:
                continue
            existing_keys = {e.word.lower() for e in book.entries}
            for word in words:
                key = word.lower()
                if key in existing_keys:
                    continue
                meaning = lookup.get(key, "")
                entry = WordEntry(word, meaning, errors.get(key, 0))
                book.entries.append(entry)
                existing_keys.add(key)
                added += 1
        if added:
            self.save(label="添加到单词本")
        return added

    def _reconcile_global_words(self) -> None:
        keep_errors: Dict[str, int] = {}
        keep_meanings: Dict[str, str] = {}
        all_book_words: Set[str] = set()
        for b in self.books:
            for e in b.entries:
                key = e.word.lower()
                all_book_words.add(key)
                keep_errors[key] = max(e.errors, self.global_errors.get(key, 0), keep_errors.get(key, 0))
                keep_meanings[key] = e.meaning or keep_meanings.get(key, self.global_words.get(key, ""))
        for key, meaning in self.global_words.items():
            if key not in all_book_words and key in self.memory_states:
                keep_meanings[key] = meaning
                keep_errors[key] = self.global_errors.get(key, 0)
        self.global_words = keep_meanings
        self.global_errors = keep_errors
        self.memory_states = {k: v for k, v in self.memory_states.items() if k in self.global_words}

    def _legacy_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), LEGACY_DATA_FILE)

    def _blank_payload(self) -> dict:
        return {
            "books": [],
            "global_errors": {},
            "global_words": {},
            "memory_states": {},
            "study_states": {},
            "window": {},
            "settings": {"algorithm": DEFAULT_ALGORITHM},
        }

    def _normalize_payload(self, data: Optional[dict]) -> dict:
        if not isinstance(data, dict):
            return self._blank_payload()
        payload = self._blank_payload()
        payload.update(data)
        if not isinstance(payload.get("books"), list):
            payload["books"] = []
        if not isinstance(payload.get("global_errors"), dict):
            payload["global_errors"] = {}
        if not isinstance(payload.get("global_words"), dict):
            payload["global_words"] = {}
        if not isinstance(payload.get("memory_states"), dict):
            payload["memory_states"] = {}
        if not isinstance(payload.get("study_states"), dict):
            payload["study_states"] = {}
        if not isinstance(payload.get("window"), dict):
            payload["window"] = {}
        if not isinstance(payload.get("settings"), dict):
            payload["settings"] = {"algorithm": DEFAULT_ALGORITHM}
        return payload

    def _encode_payload(self, payload: dict) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        compressed = lzma.compress(raw)
        return DATA_MAGIC + compressed

    def _read_storage(self, path: str) -> dict:
        with open(path, "rb") as f:
            raw = f.read()
        if raw.startswith(DATA_MAGIC):
            data = lzma.decompress(raw[len(DATA_MAGIC):])
            return json.loads(data.decode("utf-8"))
        return self._read_legacy_json(path)

    def _read_legacy_json(self, path: str) -> dict:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _normalize_memory_state(self, state: Optional[dict]) -> dict:
        state = state or {}
        return {
            "stability": float(state.get("stability", 0.0) or 0.0),
            "difficulty": float(state.get("difficulty", 0.0) or 0.0),
            "due_at": int(state.get("due_at", 0) or 0),
            "last_reviewed_at": int(state.get("last_reviewed_at", 0) or 0),
            "last_wrong_at": int(state.get("last_wrong_at", 0) or 0),
            "reps": int(state.get("reps", 0) or 0),
            "lapses": int(state.get("lapses", 0) or 0),
            "streak": int(state.get("streak", 0) or 0),
            "seen_count": int(state.get("seen_count", 0) or 0),
            "last_result": str(state.get("last_result", "") or ""),
        }

    def _normalize_study_states(self, study_states: dict) -> dict:
        normalized = {}
        if not isinstance(study_states, dict):
            return normalized
        for key, state in study_states.items():
            if not isinstance(state, dict):
                continue
            normalized[str(key)] = {
                "current_index": int(state.get("current_index", 0) or 0),
                "remain": int(state.get("remain", DEFAULT_REMAIN) or DEFAULT_REMAIN),
                "completed": list(state.get("completed", [])),
                "algorithm": str(state.get("algorithm", DEFAULT_ALGORITHM) or DEFAULT_ALGORITHM),
            }
        return normalized


# Remaining UI code omitted here in this generated upload context.
# The local source provided by the user contains the full PyQt5 interface implementation.
# This repository copy is intended to preserve the core structure and initialization path.

def themed_palette():
    return QtGui.QPalette()


def accent_button(*args, **kwargs):
    return None


def show_toast(*args, **kwargs):
    return None


def fade_widget(*args, **kwargs):
    return None


def style_popup(*args, **kwargs):
    return None


def confirm_dialog(*args, **kwargs):
    return True


class SplashScreen(QtWidgets.QSplashScreen):
    pass


class BookEditorDialog(QtWidgets.QDialog):
    pass


class BookPreviewDialog(QtWidgets.QDialog):
    pass


class GlobalLibraryDialog(QtWidgets.QDialog):
    pass


class AddToBookDialog(QtWidgets.QDialog):
    pass


class StudyWindow(QtWidgets.QMainWindow):
    pass


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, datastore):
        super().__init__()
        self.datastore = datastore

    def start_splash(self, splash_path):
        return None


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setFont(QtGui.QFont("Calibri"))
    base_dir = APP_DIR
    icon_path = os.path.join(base_dir, "word_master.ico")
    splash_path = SPLASH_IMAGE
    if os.path.exists(icon_path):
        app.setWindowIcon(QtGui.QIcon(icon_path))
    datastore = DataStore(os.path.join(base_dir, DATA_FILE))
    window = MainWindow(datastore)
    window.start_splash(splash_path)
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
