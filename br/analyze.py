"""From a patch to the tests it could break, using maps built from the pinned commit."""

from __future__ import annotations

import bisect
import os
import pickle
import re

from br import patch as patchlib
from br import pyindex

# Files pytest itself reads: a change here can alter every test's outcome.
PYTEST_CONFIG = {"pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg", "conftest.py", ".pytest.ini", "pytest.toml", ".pytest.toml"}


_HELPERS = []


def _loads_like_before(root, base_text, new_text):
    """False if one of the suite's fixture loaders accepts the old text but not the new.

    A fixture that no longer loads takes down every test of a class that loads
    it in setUpClass, whether or not the test uses it.
    """
    if not _HELPERS:
        try:
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "_br_fixture_helpers", os.path.join(root, "tests", "helpers.py")
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _HELPERS.append(module)
        except Exception:  # noqa: BLE001
            _HELPERS.append(None)
    helpers = _HELPERS[0]
    if helpers is None:
        return False
    path = f"/tmp/br_fixture_{os.getpid()}.sql"
    try:
        for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
            loader = getattr(helpers, name, None)
            if loader is None:
                return False
            outcomes = []
            for text in (base_text, new_text):
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(text)
                try:
                    list(loader(path))
                    outcomes.append(True)
                except Exception:  # noqa: BLE001
                    outcomes.append(False)
            if outcomes[0] and not outcomes[1]:
                return False
        return True
    except OSError:
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


_PYTEST_SECTION = re.compile(r"^\s*\[(tool\.pytest|tool:pytest|pytest)\b", re.M)
_TEST_TEXT: dict = {}


def _mentions(maps, root, source):
    """Tests in files whose source names a path outside sqlglot/ and tests/.

    Such a file is reached by path (sqlglotc/setup.py is run by its test), which
    the open() records do not always see.
    """
    words = {os.path.basename(source)}
    top = source.split("/", 1)[0]
    if "/" in source and len(top) > 3:
        words.add(top)
    bits = 0
    for index, rel in enumerate(maps.files):
        text = _TEST_TEXT.get(rel)
        if text is None:
            try:
                with open(os.path.join(root, rel), encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                text = ""
            _TEST_TEXT[rel] = text
        if any(word in text for word in words):
            bits |= maps.file_bits[index]
    return bits


_ASSIGN = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?::[^=]*)?=(?!=)")


class Maps:
    def __init__(self, maps_dir):
        self.dir = maps_dir
        with open(os.path.join(maps_dir, "meta.pkl"), "rb") as handle:
            meta = pickle.load(handle)
        self.meta = meta
        self.tests = meta["tests"]
        self.files = meta["files"]
        self.all_bits = (1 << len(self.tests)) - 1
        self.file_bits = [0] * len(self.files)
        for test_index, file_index in enumerate(meta["test_file"]):
            self.file_bits[file_index] |= 1 << test_index
        self._cov = {}
        self._units = {}
        self._loaded = {}
        self.digests_ok = self._check_digests()

    def _pickle(self, name):
        if name not in self._loaded:
            with open(os.path.join(self.dir, name), "rb") as handle:
                self._loaded[name] = pickle.load(handle)
        return self._loaded[name]

    def state(self):
        """Fingerprints of import-time state, the import graph, and friends."""
        return self._pickle("state.pkl")

    def cell_readers(self):
        return self._pickle("cells.pkl")

    def names(self):
        return self._pickle("names.pkl")

    def entries(self):
        """Per-entry fingerprints of class-level dicts, or {} for older maps."""
        try:
            return self._pickle("entries.pkl")
        except OSError:
            return {}

    def key_readers(self):
        """Which tests read which entries of class-level dicts, or None."""
        try:
            return self._pickle("keys.pkl")
        except OSError:
            return None

    def _check_digests(self):
        """Recompute one indexed file's digests: they must match the maps exactly."""
        root = os.environ.get("BR_SG", "/sg")
        source = "sqlglot/serde.py"
        cov = self.cov(source)
        try:
            with open(os.path.join(root, source), encoding="utf-8") as handle:
                text = handle.read()
            tree = pyindex.code_tree(compile(text, source, "exec", dont_inherit=True))
        except (OSError, SyntaxError):
            return False
        if cov is None or "index" not in cov:
            return False
        base = cov["index"]["tree"]
        return set(base) == set(tree) and all(
            base[path]["digest"] == info["digest"] for path, info in tree.items()
        )

    def units(self, test_index):
        """Per-unit coverage of a test whose work splits into units, or None."""
        if test_index not in self._units:
            path = os.path.join(self.dir, "units", f"{test_index}.pkl")
            try:
                with open(path, "rb") as handle:
                    self._units[test_index] = pickle.load(handle)
            except OSError:
                self._units[test_index] = None
        return self._units[test_index]

    def cov(self, source):
        if source not in self._cov:
            path = os.path.join(self.dir, "cov", source.replace("/", "__") + ".pkl")
            try:
                with open(path, "rb") as handle:
                    self._cov[source] = pickle.load(handle)
            except OSError:
                self._cov[source] = None
        return self._cov[source]

    def files_to_tests(self, file_bits):
        bits = 0
        index = 0
        while file_bits:
            if file_bits & 1:
                bits |= self.file_bits[index]
            file_bits >>= 1
            index += 1
        return bits


def users_of(maps, source):
    """Every test that ran or imported any line of a source file."""
    cov = maps.cov(source)
    if cov is None:
        return maps.all_bits
    bits = 0
    for kind in ("imb", "imf", "rt"):
        for value in cov[kind].values():
            bits |= value
    file_bits = 0
    for kind in ("eb", "ef"):
        for value in cov[kind].values():
            file_bits |= value
    return bits | maps.files_to_tests(file_bits)


class Analysis:
    def __init__(self, commit):
        self.commit = commit
        self.cands = 0  # bitset over the build-time test order
        self.fallback = False  # not understood: run everything
        self.notes = []
        self.patched = {}  # relpath -> new text (None when deleted)
        self.python_changed = False
        # Import-time effects in sqlglot modules are settled by importing the patched
        # tree and comparing its state; `coarse` is what to assume if that cannot be done.
        self.coarse = 0
        self.diff_modules = set()
        self.translate = {}  # digest of a changed function's new code -> its old digest
        self.targets = {}  # source -> lines whose execution makes a test a candidate
        self.full = 0  # candidates for some other reason: they cannot be run in slices
        self.no_slice = False
        self.sure = 0  # candidates expected to fail: not worth checking
        # Function bodies can be swapped into a process that already imported sqlglot.
        self.hot = []  # (absolute file, first line, name, new code object)
        self.hot_ok = True
        self.modules = set()  # sqlglot modules whose source changed
        self.reads_source = False  # a candidate opens a patched .py file as data
        self.forced = set()  # cells assigned by the edited class-level statements
        self.unnamed = False  # a class-level edit whose target could not be named

    def note(self, text):
        self.notes.append(text)


def _or_lines(mapping, lines):
    bits = 0
    for line in lines:
        value = mapping.get(line)
        if value:
            bits |= value
    return bits


def _indent(text):
    return len(text) - len(text.lstrip(" \t"))


def _hunk_pairs(file_patch):
    """Per hunk: (removed old line numbers, removed texts, added texts, simple 1:1 replace?)."""
    result = []
    for old_start, _, _, _, lines in file_patch.hunks:
        number = old_start
        removed = []
        removed_texts = []
        added_texts = []
        shape = []
        for tag, text in lines:
            if tag == "-":
                removed.append(number)
                removed_texts.append(text)
                number += 1
                shape.append("-")
            elif tag == "+":
                added_texts.append(text)
                shape.append("+")
            else:
                number += 1
                shape.append(" ")
        core = "".join(shape).strip(" ")
        simple = (
            len(removed) == len(added_texts)
            and len(removed) > 0
            and core == "-" * len(removed) + "+" * len(removed)
            and all(
                _indent(a) == _indent(b) and a.strip() and b.strip()
                for a, b in zip(removed_texts, added_texts)
            )
        )
        result.append((removed, removed_texts, added_texts, simple, old_start))
    return result


def _span_for(spans, starts, line):
    position = bisect.bisect_right(starts, line) - 1
    while position >= 0:
        start, end, is_def = spans[position]
        if start <= line <= end:
            return spans[position]
        if end < line and start < line:
            # spans are disjoint and sorted by start; an earlier one cannot contain it either
            return None
        position -= 1
    return None


def analyze_python(analysis, maps, source, base_text, new_text, file_patch, root):
    cov = maps.cov(source)
    if cov is None or "index" not in cov:
        analysis.fallback = True
        analysis.note(f"{source}: python file without an index")
        return
    base_tree = cov["index"]["tree"]
    spans = cov["index"]["spans"]
    starts = [span[0] for span in spans]

    try:
        # From bytes, so a coding declaration is honoured the way an import would.
        new_code = compile(new_text.encode("utf-8"), source, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, UnicodeError, LookupError):
        # Nothing that imports this module can work.
        users = users_of(maps, source)
        analysis.cands |= users
        analysis.full |= users
        analysis.hot_ok = False
        if source.startswith("sqlglot/"):
            broken_module = source[:-3].replace("/", ".")
            if broken_module.endswith(".__init__"):
                broken_module = broken_module[: -len(".__init__")]
            analysis.modules.add(broken_module)
        analysis.python_changed = True
        analysis.note(f"{source}: does not compile")
        return
    new_tree = pyindex.code_tree(new_code)

    changed = [
        path
        for path, info in base_tree.items()
        if path in new_tree and new_tree[path]["digest"] != info["digest"]
    ]
    added = [path for path in new_tree if path not in base_tree]
    removed = [path for path in base_tree if path not in new_tree]
    if not changed and not added and not removed:
        analysis.note(f"{source}: same code")
        return
    analysis.python_changed = True
    if not source.startswith("sqlglot/"):
        analysis.hot_ok = False
    if added or removed:
        analysis.hot_ok = False
    # (first line, name) must pick out one code object for a hot swap to find it.
    seen_keys = {}
    for tree_path, info in base_tree.items():
        key = (info["first"], tree_path[-1][0] if tree_path else "<module>")
        seen_keys[key] = seen_keys.get(key, 0) + 1
    new_codes = {}

    def collect(node, path):
        new_codes[path] = node
        counts = {}
        for const in node.co_consts:
            if hasattr(const, "co_code"):
                index = counts.get(const.co_name, 0)
                counts[const.co_name] = index + 1
                collect(const, path + ((const.co_name, index),))

    collect(new_code, ())

    hunks = _hunk_pairs(file_patch)
    rt, imf, imb, ef, eb = cov["rt"], cov["imf"], cov["imb"], cov["ef"], cov["eb"]
    in_sqlglot = source.startswith("sqlglot/")
    module_name = source[:-3].replace("/", ".")
    if module_name.endswith(".__init__"):
        module_name = module_name[: -len(".__init__")]
    if in_sqlglot:
        analysis.modules.add(module_name)
    if in_sqlglot and (added or removed):
        analysis.diff_modules.add(module_name)

    # A code object that only exists on one side changes its parent, which is in `changed`.
    for path in changed:
        info = base_tree[path]
        low, high = info["low"], info["high"]
        if info["kind"] == "function":
            precise = True
            lines = set()
            touched = False
            for removed_lines, _, added_texts, simple, old_start in hunks:
                inside = [line for line in removed_lines if low <= line <= high]
                if not inside and not (low <= old_start <= high):
                    continue
                touched = True
                if not simple or len(inside) != len(removed_lines):
                    precise = False
                    break
                for line in inside:
                    span = _span_for(spans, starts, line)
                    if span is None or span[2]:
                        precise = False
                        break
                    lines.update(range(span[0], span[1] + 1))
                if not precise:
                    break
            if not touched or not lines:
                precise = False
            if precise:
                # The statement must have instructions of its own, or tracing cannot see it.
                own = set(info["own"])
                for child in base_tree:
                    if len(child) > len(path) and child[: len(path)] == path:
                        own.update(base_tree[child]["own"])
                if not (lines & own):
                    precise = False
            if not precise:
                lines = range(low, high + 1)
            bits = _or_lines(rt, lines)
            import_bits = _or_lines(imf, lines)
            eager = _or_lines(ef, lines)
            analysis.cands |= bits
            analysis.targets.setdefault(source, set()).update(lines)
            analysis.translate[new_tree[path]["digest"]] = info["digest"]
            # Every function on the way down to the edited one gets its new code.
            for depth in range(1, len(path) + 1):
                outer = path[:depth]
                outer_info = base_tree[outer]
                if outer_info["kind"] != "function":
                    continue
                name = outer[-1][0]
                if seen_keys.get((outer_info["first"], name), 0) != 1 or outer not in new_codes:
                    analysis.hot_ok = False
                    continue
                analysis.hot.append(
                    (os.path.join(root, source), outer_info["first"], name, new_codes[outer])
                )
            at_import = import_bits | maps.files_to_tests(eager)
            if at_import:
                if in_sqlglot:
                    analysis.coarse |= at_import
                    analysis.diff_modules.add(module_name)
                else:
                    analysis.cands |= at_import
                    analysis.full |= at_import
            analysis.note(
                f"{source}: function {path[-1][0]}@{info['first']} "
                f"{'lines' if precise else 'whole'} rt={bits.bit_count()} "
                f"imp={import_bits.bit_count()} eager_files={eager.bit_count()}"
            )
        else:
            lines = info["own"]
            bits = _or_lines(imb, lines)
            eager = _or_lines(eb, lines)
            if in_sqlglot:
                analysis.coarse |= bits | maps.files_to_tests(eager)
                analysis.diff_modules.add(module_name)
                qualname = ".".join(part for part, _ in path) or None
                own = set(lines)
                named = False
                base_lines = base_text.split("\n")
                for removed_lines, _, added_texts, _, _ in hunks:
                    # A line on its own names a cell only when it is a whole new
                    # statement; an edited line is named by the statement it is in.
                    texts = [] if removed_lines else list(added_texts)
                    for line in removed_lines:
                        if line in own or low <= line <= high:
                            span = _span_for(spans, starts, line)
                            if span is not None and span[0] in own:
                                texts.append(base_lines[span[0] - 1])
                    for text in texts:
                        match = _ASSIGN.match(text)
                        if match and (_indent(text) == 0) == (qualname is None):
                            analysis.forced.add((module_name, qualname, match.group(1)))
                            named = True
                if not named:
                    analysis.unnamed = True
            else:
                analysis.cands |= bits | maps.files_to_tests(eager)
                analysis.full |= bits | maps.files_to_tests(eager)
            analysis.note(
                f"{source}: {info['kind']} body {path[-1][0] if path else '<module>'} "
                f"lazy={bits.bit_count()} eager_files={eager.bit_count()}"
            )


def analyze(commit, patch_text, maps, root):
    analysis = Analysis(commit)
    if not maps.digests_ok:
        analysis.fallback = True
        analysis.note("maps do not match this interpreter")
        return analysis
    try:
        file_patches = patchlib.parse(patch_text)
    except patchlib.PatchError as error:
        analysis.fallback = True
        analysis.note(f"unparsable patch: {error}")
        return analysis
    if not file_patches:
        analysis.note("empty patch")
        return analysis

    tests_python = any(
        (file_patch.path or "").startswith("tests/") and (file_patch.path or "").endswith(".py")
        for file_patch in file_patches
    )
    for file_patch in file_patches:
        source = file_patch.path
        if source is None:
            analysis.fallback = True
            analysis.note("patch without a path")
            continue
        base_path = os.path.join(root, file_patch.old_path or source)
        if (
            file_patch.binary
            or file_patch.rename
            or file_patch.new_file
            or file_patch.deleted
            or file_patch.old_path != file_patch.new_path
            or not os.path.isfile(base_path)
        ):
            analysis.fallback = True
            analysis.note(f"{source}: added, removed, renamed or binary")
            continue
        if not file_patch.hunks:
            # A mode change alone cannot alter what a test sees.
            analysis.note(f"{source}: no content change")
            continue
        try:
            with open(base_path, encoding="utf-8", newline="") as handle:
                base_text = handle.read()
            new_text = patchlib.apply(base_text, file_patch)
        except (OSError, UnicodeDecodeError, patchlib.PatchError) as error:
            analysis.fallback = True
            analysis.note(f"{source}: patch does not apply ({error})")
            continue
        if new_text == base_text:
            analysis.note(f"{source}: unchanged")
            continue
        analysis.patched[source] = new_text

        # Whoever reads the file as data sees the new text.
        opened = maps.meta["opens"].get(source, 0)
        opened |= maps.files_to_tests(maps.meta["eager_opens"].get(source, 0))
        users = maps.meta.get("fixture_users", {}).get(source)
        if (
            users is not None
            and not tests_python
            and _loads_like_before(root, base_text, new_text)
        ):
            # Tests that only see this fixture because their class loads it for
            # another test do not depend on what it says.
            analysis.note(f"{source}: {users.bit_count()} of {opened.bit_count()} openers use it")
            opened = users
        if opened:
            analysis.cands |= opened
            analysis.full |= opened
            if source.endswith(".py"):
                analysis.reads_source = True
            if source.startswith("tests/fixtures/"):
                # A test whose fixture was edited almost always fails.
                analysis.sure |= opened
            analysis.note(f"{source}: opened by {opened.bit_count()} tests")

        name = os.path.basename(source)
        if source.startswith("tests/"):
            analysis.no_slice = True
        if not (source.endswith(".py") and source.startswith("sqlglot/")):
            analysis.hot_ok = False
        if source.endswith(".py") and (source.startswith("sqlglot/") or source.startswith("tests/")):
            if name == "conftest.py":
                analysis.fallback = True
                analysis.note(f"{source}: pytest config")
                continue
            analyze_python(analysis, maps, source, base_text, new_text, file_patch, root)
        elif name in PYTEST_CONFIG and (
            name in ("pytest.ini", "conftest.py", ".pytest.ini", "pytest.toml", ".pytest.toml")
            or _PYTEST_SECTION.search(new_text)
            or _PYTEST_SECTION.search(base_text)
        ):
            analysis.fallback = True
            analysis.note(f"{source}: pytest config")
        elif not source.startswith(("sqlglot/", "tests/")):
            mentioned = _mentions(maps, root, source)
            analysis.cands |= mentioned
            analysis.full |= mentioned
            analysis.note(f"{source}: outside the tree, named by {mentioned.bit_count()} tests")
        else:
            analysis.note(f"{source}: data file")
    return analysis
