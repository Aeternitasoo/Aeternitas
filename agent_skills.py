"""Metadata-first discovery and bounded, version-bound SKILL.md loading."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from harness_contracts import HarnessError


@dataclass(frozen=True)
class SkillRegistry:
    root: Path

    def _path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,62}', name):
            raise HarnessError('invalid_skill_name')
        root = Path(self.root)
        path = root / name / 'SKILL.md'
        if root.is_symlink() or (root/name).is_symlink() or path.is_symlink():
            raise HarnessError('skill_symlink')
        if not path.resolve().is_relative_to(root.resolve()):
            raise HarnessError('skill_path_escape')
        return path

    def _metadata(self, name):
        path = self._path(name)
        try:
            with path.open('rb') as stream:
                if stream.readline(128).strip() != b'---':
                    raise HarnessError('skill_frontmatter')
                fields, size = {}, 0
                for _ in range(12):
                    line = stream.readline(1024); size += len(line)
                    if size > 4096 or not line:
                        raise HarnessError('skill_frontmatter')
                    if line.strip() == b'---':
                        break
                    key, sep, value = line.decode('utf-8').strip().partition(':')
                    if not sep or key in fields or key not in {'name', 'description'}:
                        raise HarnessError('skill_metadata_fields')
                    value = value.strip()
                    fields[key] = json.loads(value) if value.startswith('"') else value
                else:
                    raise HarnessError('skill_frontmatter')
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise HarnessError('skill_unreadable') from None
        if (set(fields) != {'name','description'} or fields['name'] != name
                or any(not isinstance(v, str) or not v.strip() for v in fields.values())
                or len(fields['description']) > 500):
            raise HarnessError('skill_metadata_invalid')
        return fields

    def discover(self):
        root = Path(self.root)
        if root.is_symlink():
            raise HarnessError('skill_symlink')
        found = []
        for child in sorted(root.iterdir()):
            if child.is_dir() and (child/'SKILL.md').exists():
                found.append(self._metadata(child.name))
        if len(found) > 16:
            raise HarnessError('skill_catalog_limit')
        return found

    def load(self, name, expected_metadata):
        if name not in expected_metadata or self._metadata(name) != expected_metadata[name]:
            raise HarnessError('skill_metadata_changed_or_unregistered')
        path = self._path(name)
        with path.open('rb') as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise HarnessError('skill_size_limit')
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            raise HarnessError('skill_encoding') from None
        version = hashlib.sha256(raw).hexdigest()
        return {**expected_metadata[name], 'version': version, 'sha256': version, 'content': text}


def load_skill(*, registry, metadata, name):
    return registry.load(name, metadata)
