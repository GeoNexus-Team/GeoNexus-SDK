"""Recipe loading and saving (YAML or JSON).

同样的取舍与 :mod:`geonexus.geocard.loader` 一致：配方是**文本契约**，必须能进
git、能被人 review、能在没有 SDK 的环境里被读懂，所以不引入自定义二进制格式。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from .model import Recipe, RecipeError

__all__ = ["parse_recipe_file", "load_recipe", "load_recipe_dict", "save_recipe"]


def parse_recipe_file(path: str | os.PathLike) -> dict[str, Any]:
    """把 YAML/JSON 文件读成字典（不做契约校验）。"""
    target = Path(path)
    if not target.exists():
        raise RecipeError(f"recipe file not found: {target}")
    text = target.read_text(encoding="utf-8")
    try:
        if target.suffix.lower() in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        elif target.suffix.lower() == ".json":
            data = json.loads(text)
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = yaml.safe_load(text)
    except Exception as exc:  # pragma: no cover - defensive
        raise RecipeError(f"failed to parse recipe file {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise RecipeError(f"recipe file {target} must contain a mapping")
    return data


def load_recipe(path: str | os.PathLike) -> Recipe:
    """读取并校验一个配方。校验失败抛 :class:`RecipeValidationError`。"""
    return Recipe.from_dict(parse_recipe_file(path))


def load_recipe_dict(path: str | os.PathLike) -> dict[str, Any]:
    """原始字典，供 ``recipe inspect`` 这类只读展示使用。"""
    return parse_recipe_file(path)


def save_recipe(recipe: Recipe, path: str | os.PathLike) -> Path:
    """按扩展名写 YAML 或 JSON；写之前先自校验，避免落盘一个坏配方。"""
    recipe.validate()
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = recipe.to_dict()
    if target.suffix.lower() == ".json":
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    else:
        target.write_text(recipe.to_yaml(), encoding="utf-8")
    return target
