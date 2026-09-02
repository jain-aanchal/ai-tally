# SPDX-License-Identifier: Apache-2.0
"""Recipe catalog loader (CTO-261 section 5.1).

Reads ``sdk/python/recipes/`` (data, not code): one YAML file per recipe plus
``index.yaml`` and ``schema.json``. Kept beside the SDK so a recipe and the method
it targets move together (section 5.1). This module is the single in-process view of
the catalog that every MCP tool reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# recipes/ sits next to onboarding_mcp/ under sdk/python/.
RECIPES_DIR = Path(__file__).resolve().parent.parent / "recipes"


@dataclass(frozen=True)
class Recipe:
    """One parsed recipe. ``raw`` is the full document; the rest are convenience views."""

    id: str
    kind: str
    title: str
    detect: dict[str, Any]
    sdk_surface: dict[str, Any]
    edit: dict[str, Any]
    verify: dict[str, Any]
    notes: str
    raw: dict[str, Any]

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> Recipe:
        return cls(
            id=doc["id"],
            kind=doc["kind"],
            title=doc["title"],
            detect=doc.get("detect", {}),
            sdk_surface=doc.get("sdk_surface", {}),
            edit=doc.get("edit", {}),
            verify=doc.get("verify", {}),
            notes=doc.get("notes", ""),
            raw=doc,
        )

    @property
    def imports(self) -> list[str]:
        return list(self.detect.get("imports", []))

    @property
    def call_patterns(self) -> list[str]:
        return list(self.detect.get("call_patterns", []))

    @property
    def layer(self) -> str:
        return str(self.sdk_surface.get("layer", ""))


class RecipeCatalog:
    """In-memory catalog: id -> Recipe, plus lookups the MCP tools need."""

    def __init__(self, recipes: list[Recipe], schema: dict[str, Any]):
        self._by_id = {r.id: r for r in recipes}
        self.schema = schema

    @property
    def recipes(self) -> list[Recipe]:
        return list(self._by_id.values())

    def get(self, recipe_id: str) -> Recipe | None:
        return self._by_id.get(recipe_id)

    def by_kind(self, kind: str) -> list[Recipe]:
        return [r for r in self._by_id.values() if r.kind == kind]

    def resolve_alias(self, name: str) -> Recipe | None:
        """Resolve a recipe by id, or by a framework / provider name in its id or detect imports.

        Lets ``get_recipe`` take a friendly name ("pinecone", "fastapi") as section 4.2 allows,
        not only the dotted id.
        """
        exact = self.get(name)
        if exact is not None:
            return exact
        needle = name.lower()
        for recipe in self._by_id.values():
            if needle in recipe.id.lower():
                return recipe
            if any(needle == imp.lower() for imp in recipe.imports):
                return recipe
            if needle == str(recipe.detect.get("web_framework", "")).lower():
                return recipe
        return None


def _load_index(recipes_dir: Path) -> list[str]:
    index = yaml.safe_load((recipes_dir / "index.yaml").read_text())
    return [entry["file"] for entry in index["recipes"]]


@lru_cache(maxsize=1)
def get_catalog() -> RecipeCatalog:
    """Load and cache the catalog from ``RECIPES_DIR``."""
    return load_catalog(RECIPES_DIR)


def load_catalog(recipes_dir: Path) -> RecipeCatalog:
    """Load a catalog from an explicit directory (used by tests and by ``get_catalog``)."""
    schema = json.loads((recipes_dir / "schema.json").read_text())
    recipes: list[Recipe] = []
    for filename in _load_index(recipes_dir):
        doc = yaml.safe_load((recipes_dir / filename).read_text())
        recipes.append(Recipe.from_doc(doc))
    return RecipeCatalog(recipes, schema)
