"""Recipe —— 可执行方案（DAG）。

GeoCard 说"这是什么"，Recipe 说"怎么跑"，TaskGraph 是一次**运行**。
三者串起来，一个复杂案例才既能被读懂，又能被复现，还能被改参数复用。

    from geonexus.recipe import load_recipe, materialize

    recipe = load_recipe("sdg-15-3-1.yaml")
    plan = materialize(recipe, {"year": 2021, "region": "墨西哥"}, run_id="run-42")
    plan.graph.topological_order()   # ['run-42:s1', 'run-42:s2', 'run-42:s3']
    plan.output_refs()               # {'report': 'step://s3/report'}
"""

from ..review import (
    REVIEW_STATUSES,
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
)
from .loader import load_recipe, load_recipe_dict, parse_recipe_file, save_recipe
from .materialize import (
    PARAM_TEMPLATE_RE,
    RunPlan,
    coerce_param_value,
    deliverables,
    materialize,
    resolve_params,
)
from .model import (
    RECIPE_ADDRESS_RE,
    RECIPE_VERSION,
    STEP_KINDS,
    Recipe,
    RecipeError,
    RecipeOutput,
    RecipeParamError,
    RecipeStep,
    RecipeValidationError,
    format_recipe_address,
    parse_recipe_address,
)
from .registry import (
    STATUSES,
    RecipeAmbiguous,
    RecipeConflict,
    RecipeEntry,
    RecipeNotFound,
    RecipeRegistry,
    version_key,
)

__all__ = [
    "Recipe",
    "RecipeStep",
    "RecipeOutput",
    "RecipeError",
    "RecipeValidationError",
    "RecipeParamError",
    "RECIPE_VERSION",
    "STEP_KINDS",
    "RECIPE_ADDRESS_RE",
    "parse_recipe_address",
    "format_recipe_address",
    "load_recipe",
    "load_recipe_dict",
    "parse_recipe_file",
    "save_recipe",
    "materialize",
    "deliverables",
    "resolve_params",
    "coerce_param_value",
    "PARAM_TEMPLATE_RE",
    "RunPlan",
    "RecipeRegistry",
    "RecipeEntry",
    "RecipeConflict",
    "RecipeNotFound",
    "RecipeAmbiguous",
    "version_key",
    "STATUSES",
    "STATUS_PENDING",
    "STATUS_APPROVED",
    "STATUS_REJECTED",
    "REVIEW_STATUSES",
]
