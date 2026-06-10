#!/usr/bin/env python3
import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML is required (python3 -m pip install pyyaml)", file=sys.stderr)
    sys.exit(2)


PATCH_VERSION = 5


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(
            f"Command failed ({' '.join(cmd)}) in {cwd or os.getcwd()}\n"
            f"stdout:\n{p.stdout}\nstderr:\n{p.stderr}"
        )
    return p


def run_with_retries(cmd, cwd=None, attempts=3):
    last = None
    for attempt in range(1, attempts + 1):
        last = run(cmd, cwd=cwd, check=False)
        if last.returncode == 0:
            return last
        if attempt < attempts:
            print(
                f"[retry] {' '.join(cmd)} failed in {cwd or os.getcwd()} "
                f"({attempt}/{attempts}); retrying",
                file=sys.stderr,
            )
    raise RuntimeError(
        f"Command failed after {attempts} attempts ({' '.join(cmd)}) in {cwd or os.getcwd()}\n"
        f"stdout:\n{last.stdout}\nstderr:\n{last.stderr}"
    )


def load_yaml(path: Path):
    # Some upstream Rime schemas contain literal tabs before comments. Rime tolerates
    # them, but PyYAML follows the YAML spec strictly, so normalize before parsing.
    data = yaml.safe_load(path.read_text(encoding="utf-8").replace("\t", " "))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} root must be a mapping")
    return data


def load_state(path: Path):
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError("state.json must be a JSON object")
    return data


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def sha256_file(path: Path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_rel(rel):
    rel_norm = Path(rel).as_posix().strip("/")
    if rel_norm in ("", ".") or rel_norm.startswith("../") or "/../" in rel_norm:
        raise RuntimeError(f"Invalid relative path: {rel}")
    return rel_norm


def is_match(rel_path: str, patterns):
    rel = rel_path.strip("/")
    return any(fnmatch.fnmatch(rel, pat.strip("/")) for pat in patterns)


def expand_source_files(repo_root: Path, entries, excludes):
    files = set()
    for entry in entries:
        entry = normalize_rel(entry)
        if any(ch in entry for ch in "*?["):
            for path in repo_root.glob(entry):
                if path.is_file():
                    files.add(path.relative_to(repo_root).as_posix())
        else:
            path = repo_root / entry
            if path.is_dir():
                for child in path.rglob("*"):
                    if child.is_file():
                        files.add(child.relative_to(repo_root).as_posix())
            elif path.is_file():
                files.add(entry)
            else:
                raise RuntimeError(f"Missing source file: {path}")
    return sorted(rel for rel in files if not is_match(rel, excludes))


def git_prepare_repo(repo_root: Path):
    if not (repo_root / ".git").exists():
        raise RuntimeError(f"Repo missing .git: {repo_root}")

    status = run(["git", "status", "--porcelain"], cwd=repo_root).stdout.strip()
    if status:
        raise RuntimeError(f"Repo is dirty: {repo_root}")

    upstream = run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=repo_root,
    ).stdout.strip()
    if not upstream:
        raise RuntimeError(f"Repo has no upstream: {repo_root}")

    run_with_retries(["git", "fetch", "--prune", "--tags", "--force"], cwd=repo_root)

    counts = run(
        ["git", "rev-list", "--left-right", "--count", f"{upstream}...HEAD"],
        cwd=repo_root,
    ).stdout.strip()
    behind_s, ahead_s = counts.split()
    behind = int(behind_s)
    ahead = int(ahead_s)

    if ahead > 0 and behind > 0:
        raise RuntimeError(f"Repo diverged from upstream: {repo_root}")
    if ahead > 0:
        raise RuntimeError(f"Repo is ahead of upstream: {repo_root}")

    updated = False
    if behind > 0:
        run(["git", "pull", "--ff-only"], cwd=repo_root)
        updated = True

    head = run(["git", "rev-parse", "HEAD"], cwd=repo_root).stdout.strip()
    return {"head": head, "upstream": upstream, "updated": updated}


def copy_to_stage(stage_root: Path, source_root: Path, rel_path: str):
    src = source_root / rel_path
    if not src.is_file():
        raise RuntimeError(f"Missing source file: {src}")
    dst = stage_root / rel_path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def strip_utf8_bom(path: Path):
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        path.write_bytes(raw[3:])


def patch_pin_cand_filter(path: Path):
    if not path.exists():
        return

    text = path.read_text(encoding="utf-8")
    if "local function split_texts(text, delimiter)" not in text:
        text = text.replace(
            "local M = {}\n",
            """local function split_texts(text, delimiter)
    local parts = {}
    local start = 1
    while true do
        local first, last = text:find(delimiter, start, true)
        if not first then
            table.insert(parts, text:sub(start))
            break
        end
        table.insert(parts, text:sub(start, first - 1))
        start = last + 1
    end
    return parts
end

local M = {}
""",
        )

    text = text.replace(
        """            for text in texts:gmatch("[^" .. delimiter .. "]+") do
                table.insert(env.pin_cands[preedit_no_spaces], text)
            end
""",
        """            for _, text in ipairs(split_texts(texts, delimiter)) do
                if text ~= "" then
                    table.insert(env.pin_cands[preedit_no_spaces], text)
                end
            end
""",
    )
    text = text.replace(
        """                            for text in texts:gmatch("[^" .. delimiter .. "]+") do
                                table.insert(env.pin_cands[p], text)
                            end
""",
        """                            for _, text in ipairs(split_texts(texts, delimiter)) do
                                if text ~= "" then
                                    table.insert(env.pin_cands[p], text)
                                end
                            end
""",
    )
    path.write_text(text, encoding="utf-8")


def remove_from_sequence(seq, values):
    if not isinstance(seq, list):
        return
    seq[:] = [item for item in seq if item not in values]


def append_unique(seq, values):
    if not isinstance(seq, list):
        raise RuntimeError("Expected list while patching schema")
    for value in values:
        if value not in seq:
            seq.append(value)


def patch_huma_schema(path: Path):
    data = load_yaml(path)
    schema = data.get("schema", {})
    schema_id = schema.get("schema_id")
    if schema_id not in ("tiger", "tigress"):
        raise RuntimeError(f"Unexpected huma schema id in {path}: {schema_id}")

    deps = schema.setdefault("dependencies", [])
    remove_from_sequence(deps, ["PY_c"])
    append_unique(
        deps,
        [
            "rime_frost",
            "rime_frost_double_pinyin",
            "rime_frost_double_pinyin_flypy",
            "rime_frost_double_pinyin_mspy",
            "stroke",
        ],
    )

    translator = data.setdefault("translator", {})
    translator["dictionary"] = f"{schema_id}.extended"
    translator.setdefault("prism", schema_id)

    engine = data.setdefault("engine", {})
    segmentors = engine.setdefault("segmentors", [])
    lookup_segmentors = [
        "affix_segmentor@frost_pinyin_lookup",
        "affix_segmentor@frost_zrm_lookup",
        "affix_segmentor@frost_flypy_lookup",
        "affix_segmentor@frost_mspy_lookup",
        "affix_segmentor@stroke_lookup",
    ]
    segmentor_insert_at = (
        segmentors.index("abc_segmentor")
        if "abc_segmentor" in segmentors
        else len(segmentors)
    )
    for name in reversed(lookup_segmentors):
        if name not in segmentors:
            segmentors.insert(segmentor_insert_at, name)

    translators = engine.setdefault("translators", [])
    remove_from_sequence(translators, ["reverse_lookup_translator"])
    lookup_translators = [
        "script_translator@frost_pinyin_lookup",
        "script_translator@frost_zrm_lookup",
        "script_translator@frost_flypy_lookup",
        "script_translator@frost_mspy_lookup",
        "table_translator@stroke_lookup",
    ]
    insert_at = 1 if "punct_translator" in translators else 0
    for name in reversed(lookup_translators):
        if name not in translators:
            translators.insert(insert_at, name)

    filters = engine.setdefault("filters", [])
    lookup_filters = [
        "reverse_lookup_filter@frost_pinyin_reverse_lookup",
        "reverse_lookup_filter@frost_zrm_reverse_lookup",
        "reverse_lookup_filter@frost_flypy_reverse_lookup",
        "reverse_lookup_filter@frost_mspy_reverse_lookup",
        "reverse_lookup_filter@stroke_reverse_lookup",
    ]
    uniquifier_index = filters.index("uniquifier") if "uniquifier" in filters else len(filters)
    for name in lookup_filters:
        if name not in filters:
            filters.insert(uniquifier_index, name)
            uniquifier_index += 1

    data["reverse_lookup"] = {
        "tag": "frost_pinyin_lookup",
        "dictionary": "rime_frost",
        "prism": "rime_frost",
        "prefix": "`",
        "tips": "〔白霜全拼反查〕",
        "closing_tips": "〔反查关闭〕",
        "enable_user_dict": False,
        "enable_completion": True,
        "comment_format": ["erase/^.*$/"],
        "preedit_format": [
            "xform/([nl])v/$1ü/",
            "xform/([nl])ue/$1üe/",
            "xform/([jqxy])v/$1u/",
        ],
    }

    lookup_nodes = {
        "frost_pinyin_lookup": ("rime_frost", "rime_frost", "`", "〔白霜全拼反查〕"),
        "frost_zrm_lookup": (
            "rime_frost",
            "rime_frost_double_pinyin",
            "`Z",
            "〔白霜自然码反查〕",
        ),
        "frost_flypy_lookup": (
            "rime_frost",
            "rime_frost_double_pinyin_flypy",
            "`X",
            "〔白霜小鹤反查〕",
        ),
        "frost_mspy_lookup": (
            "rime_frost",
            "rime_frost_double_pinyin_mspy",
            "`M",
            "〔白霜微软反查〕",
        ),
    }
    for node, (dictionary, prism, prefix, tips) in lookup_nodes.items():
        data[node] = {
            "tag": node,
            "dictionary": dictionary,
            "prism": prism,
            "prefix": prefix,
            "tips": tips,
            "closing_tips": "〔反查关闭〕",
            "enable_user_dict": False,
            "enable_completion": True,
            "comment_format": ["erase/^.*$/"],
        }

    data["stroke_lookup"] = {
        "tag": "stroke_lookup",
        "dictionary": "stroke",
        "prefix": "`B",
        "tips": "〔五筆畫反查〕",
        "closing_tips": "〔反查关闭〕",
        "enable_user_dict": False,
        "enable_completion": True,
        "initial_quality": 0.5,
        "preedit_format": [
            "xform/^([hspnz]+)$/$1\\t（\\U$1\\E）/",
            "xlit/HSPNZ/一丨丿丶乙/",
        ],
        "comment_format": ["erase/^.*$/"],
    }

    for name, tag in [
        ("frost_pinyin_reverse_lookup", "frost_pinyin_lookup"),
        ("frost_zrm_reverse_lookup", "frost_zrm_lookup"),
        ("frost_flypy_reverse_lookup", "frost_flypy_lookup"),
        ("frost_mspy_reverse_lookup", "frost_mspy_lookup"),
        ("stroke_reverse_lookup", "stroke_lookup"),
    ]:
        data[name] = {
            "tags": [tag],
            "dictionary": "tigress.extended",
            "overwrite_comment": True,
        }

    recognizer = data.setdefault("recognizer", {})
    patterns = recognizer.setdefault("patterns", {})
    patterns["reverse_lookup"] = "^`([a-z]+'?)*$"
    patterns["frost_pinyin_lookup"] = "^`([a-z]+'?)*$"
    patterns["frost_zrm_lookup"] = "^`Z([a-z]+'?)*$"
    patterns["frost_flypy_lookup"] = "^`X([a-z]+'?)*$"
    patterns["frost_mspy_lookup"] = "^`M([a-z;]+'?)*$"
    patterns["stroke_lookup"] = "^`B([hspnz]+'?)*$"

    for node in ("pinyin", "chaifen"):
        tags = data.get(node, {}).get("tags")
        if isinstance(tags, list):
            append_unique(
                tags,
                [
                    "frost_pinyin_lookup",
                    "frost_zrm_lookup",
                    "frost_flypy_lookup",
                    "frost_mspy_lookup",
                    "stroke_lookup",
                ],
            )

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )


def patch_stroke_schema(path: Path):
    data = load_yaml(path)
    schema = data.get("schema", {})
    if schema.get("schema_id") != "stroke":
        raise RuntimeError(f"Unexpected stroke schema id in {path}: {schema.get('schema_id')}")

    schema.pop("dependencies", None)
    translators = data.get("engine", {}).get("translators", [])
    remove_from_sequence(translators, ["reverse_lookup_translator"])
    data.pop("reverse_lookup", None)
    data.get("recognizer", {}).get("patterns", {}).pop("reverse_lookup", None)
    extra_tags = data.get("abc_segmentor", {}).get("extra_tags")
    remove_from_sequence(extra_tags, ["reverse_lookup"])

    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )


def copy_maintained_files(destination: Path):
    archived = destination / "bak" / "20260527-pre-huma-rime"
    for name in ("installation.yaml", "user.yaml"):
        src = archived / name
        dst = destination / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


def collect_stage(manifest, stage: Path):
    sources = manifest.get("sources", {})
    conflicts = manifest.get("conflicts", {})
    dest_owners = {}
    managed_files = []

    for source_id, source in sources.items():
        repo_root = Path(source["root"]).expanduser().resolve()
        rels = expand_source_files(repo_root, source.get("files", []), source.get("exclude", []))
        for rel in rels:
            owner = dest_owners.get(rel)
            if owner is not None and owner != source_id:
                conflict = conflicts.get(rel)
                if not conflict or conflict.get("owner") not in (owner, source_id):
                    raise RuntimeError(f"Undeclared conflict for {rel}: {owner} vs {source_id}")
                if conflict.get("owner") != source_id:
                    continue
            dest_owners[rel] = source_id
            copy_to_stage(stage, repo_root, rel)
            managed_files.append(rel)

    for rel in manifest.get("schema_patches", {}).get("huma_rime", []):
        patch_huma_schema(stage / rel)
    for rel in manifest.get("schema_patches", {}).get("stroke", []):
        patch_stroke_schema(stage / rel)
    for rel in managed_files:
        if rel.endswith(".lua"):
            strip_utf8_bom(stage / rel)
    patch_pin_cand_filter(stage / "lua" / "pin_cand_filter.lua")

    return sorted(set(managed_files))


def file_differs(src: Path, dst: Path):
    if not dst.exists() or not dst.is_file():
        return True
    return sha256_file(src) != sha256_file(dst)


def manifest_digest(path: Path):
    return sha256_text(path.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Rime source sync and local patcher")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("rime-sync-manifest.yaml"),
    )
    parser.add_argument("--state", type=Path, default=Path(__file__).with_name("state.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-sync", action="store_true")
    parser.add_argument("--no-deploy", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest.resolve()
    state_path = args.state.resolve()
    manifest = load_yaml(manifest_path)
    destination = Path(manifest["destination"]).expanduser().resolve()

    source_heads = {}
    source_updated = False
    for source_id, source in manifest.get("sources", {}).items():
        repo_root = Path(source["root"]).expanduser().resolve()
        info = git_prepare_repo(repo_root)
        source_heads[source_id] = info["head"]
        source_updated = source_updated or info["updated"]
        print(f"[git] {source_id}: head={info['head']} updated={'yes' if info['updated'] else 'no'}")

    state = load_state(state_path)
    current_signature = {
        "sources": source_heads,
        "manifest_digest": manifest_digest(manifest_path),
        "patch_version": PATCH_VERSION,
    }
    if (
        not args.force_sync
        and not source_updated
        and state.get("signature") == current_signature
    ):
        print("[done] no upstream, manifest, or patch changes; skip sync")
        return 0

    with tempfile.TemporaryDirectory(prefix="rime-sync-stage-", dir=str(destination)) as tmp:
        stage = Path(tmp)
        managed_files = collect_stage(manifest, stage)

        to_copy = [
            rel for rel in managed_files if file_differs(stage / rel, destination / rel)
        ]

        print("[plan] copy/overwrite:")
        for rel in to_copy:
            print(f"  + {rel}")
        print("[plan] delete:")
        print("  (none)")

        if args.dry_run:
            print("[dry-run] no files changed")
            return 0

        destination.mkdir(parents=True, exist_ok=True)
        copy_maintained_files(destination)

        for rel in to_copy:
            src = stage / rel
            dst = destination / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    changed = bool(to_copy)
    if changed and not args.no_deploy:
        shared = manifest.get("deploy", {}).get("shared_data_dir", "/usr/share/rime-data")
        deploy_cmd = [
            "rime_deployer",
            "--build",
            str(destination),
            shared,
            str(destination / "build"),
        ]
        print(f"[deploy] {' '.join(deploy_cmd)}")
        dp = subprocess.run(deploy_cmd, text=True, capture_output=True)
        if dp.returncode != 0:
            log_path = manifest_path.with_name("deploy-error.log")
            log_path.write_text(
                "STDOUT:\n" + dp.stdout + "\nSTDERR:\n" + dp.stderr,
                encoding="utf-8",
            )
            print(f"ERROR: rime_deployer failed. log: {log_path}", file=sys.stderr)
            return dp.returncode

    state_new = {
        "signature": current_signature,
        "managed_files": managed_files,
        "last_success": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    save_state(state_path, state_new)
    print("[done] sync complete")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
