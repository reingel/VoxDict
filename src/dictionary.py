import bisect
import struct
from pathlib import Path


class StarDictIfo:
    REQUIRED_KEYS = {"wordcount", "idxfilesize", "sametypesequence"}

    def __init__(self, ifo_path: Path):
        self.valid = False
        self.bookname = ""
        self.wordcount = 0
        self.idxfilesize = 0
        self.sametypesequence = ""

        lines = ifo_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if not lines or lines[0].strip() != "StarDict's dict ifo file":
            return

        meta = {}
        for line in lines[1:]:
            if "=" in line:
                key, _, value = line.partition("=")
                meta[key.strip()] = value.strip()

        if not self.REQUIRED_KEYS.issubset(meta):
            return

        self.bookname = meta.get("bookname", ifo_path.stem)
        self.wordcount = int(meta["wordcount"])
        self.idxfilesize = int(meta["idxfilesize"])
        self.sametypesequence = meta["sametypesequence"]
        self.valid = True


class StarDictIdx:
    def __init__(self, idx_path: Path):
        data = idx_path.read_bytes()
        self._entries: list[tuple[str, int, int]] = []  # (word_lower, offset, size)
        self._words: list[str] = []  # original words, same order

        pos = 0
        while pos < len(data):
            null = data.find(b"\x00", pos)
            if null == -1 or null + 8 >= len(data) + 1:
                break
            word = data[pos:null].decode("utf-8", errors="ignore")
            offset, size = struct.unpack_from(">II", data, null + 1)
            self._entries.append((word.lower(), offset, size))
            self._words.append(word)
            pos = null + 9

    def lookup(self, word: str) -> tuple[int, int] | None:
        """Return (offset, size) for exact match (case-insensitive), or None."""
        target = word.lower()
        keys = [e[0] for e in self._entries]
        idx = bisect.bisect_left(keys, target)
        if idx < len(self._entries) and self._entries[idx][0] == target:
            _, offset, size = self._entries[idx]
            return offset, size
        return None


class StarDictDict:
    def __init__(self, dict_path: Path):
        self._data = dict_path.read_bytes()

    def read(self, offset: int, size: int) -> str:
        return self._data[offset: offset + size].decode("utf-8", errors="ignore")


class StarDict:
    def __init__(self, folder: Path):
        self.valid = False
        self.bookname = ""

        # Find required files
        ifo_files = list(folder.glob("*.ifo"))
        idx_files = list(folder.glob("*.idx"))
        dict_files = [f for f in folder.glob("*.dict") if not f.suffix == ".oft"]

        if not ifo_files or not idx_files or not dict_files:
            return

        ifo = StarDictIfo(ifo_files[0])
        if not ifo.valid:
            return

        self._ifo = ifo
        self._idx = StarDictIdx(idx_files[0])
        self._dict = StarDictDict(dict_files[0])
        self.bookname = ifo.bookname
        self.valid = True

    def lookup(self, word: str) -> str | None:
        result = self._idx.lookup(word)
        if result is None:
            return None
        offset, size = result
        return self._dict.read(offset, size)


class DictionaryManager:
    def __init__(self, dictionaries_path: Path):
        self._dicts: list[StarDict] = []
        if not dictionaries_path.is_dir():
            return
        for subfolder in sorted(dictionaries_path.iterdir()):
            if subfolder.is_dir():
                d = StarDict(subfolder)
                if d.valid:
                    self._dicts.append(d)

    @property
    def count(self) -> int:
        return len(self._dicts)

    def search_all(self, word: str) -> list[tuple[str, str]]:
        """Return list of (bookname, definition) for all dictionaries that have the word."""
        results = []
        for d in self._dicts:
            definition = d.lookup(word)
            if definition is not None:
                results.append((d.bookname, definition))
        return results
