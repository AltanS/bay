"""bay.toml schema v3: load and validate one file.

Two layers. The JSON Schema in ``schemas/bay_toml.schema.json`` holds every
rule a single key or table can express: types, enums, patterns, unknown keys,
``image`` xor ``[build]``, ``volume`` xor ``from``. The checks in this module
hold the rules that span the file: one ``needs`` form per file, ``name`` vs
the environment names, duplicate domains, and what a ``[deploy.<env>]``
override changes.

The validator needs nothing but the file. It does not look for a fleet, a
framework checkout or a repo root, so it runs anywhere ``bay`` is installed.

Every violation carries a TOML path (``services.api.port``, ``mounts[0].from``,
array indexes count from 0) and a one-sentence message written for the person
editing the file.
"""

from __future__ import annotations

import difflib
import json
import re
import tomllib
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).parent / "schemas" / "bay_toml.schema.json"

#: Keys that are legal at the top of the file. A known top-level key reported
#: as unknown inside a table is almost always the scalar-after-table trap.
TOP_LEVEL_KEYS = (
    "name", "fleet", "publish", "image", "port", "command", "release", "health",
    "replicas", "memory", "update", "zero_downtime", "logs", "secrets",
)

#: Matches the start of a template expression in any string value.
_TEMPLATE_RE = re.compile(r"\$\{|\{\{|\{%")

_TYPE_WORDS = {
    "string": "a string",
    "integer": "an integer",
    "boolean": "true or false",
    "object": "a table",
    "array": "a list",
    "number": "a number",
}


@dataclass(frozen=True, order=True)
class Violation:
    """One broken rule: where it is and what to do about it."""

    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


class BayTomlError(Exception):
    """The file could not be read or parsed, so no rule could be checked."""


def load(path: str | Path) -> dict[str, Any]:
    """Parse ``path`` as TOML. Raise BayTomlError with a plain message on failure."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError as exc:
        raise BayTomlError("file not found") from exc
    except OSError as exc:
        raise BayTomlError(f"cannot read the file ({exc.strerror})") from exc
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BayTomlError("the file is not UTF-8 text") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BayTomlError(f"the file is not valid TOML: {exc}") from exc


def load_schema() -> dict[str, Any]:
    """Return the bay.toml JSON Schema."""
    schema: dict[str, Any] = json.loads(SCHEMA_PATH.read_text())
    return schema


def validate(doc: dict[str, Any]) -> list[Violation]:
    """Return every violation in ``doc``, sorted and without duplicates.

    An empty list means the file is valid.
    """
    found = set(_schema_violations(doc))
    found.update(_file_rules(doc))
    return sorted(found)


def validate_file(path: str | Path) -> list[Violation]:
    """Load and validate one file. A parse failure is one violation at ``path``."""
    try:
        doc = load(path)
    except BayTomlError as exc:
        return [Violation(str(path), str(exc))]
    return validate(doc)


# ── Path formatting ──────────────────────────────────────────────────────────

def _fmt(parts: Any) -> str:
    out = ""
    for part in parts:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else str(part)
    return out


def _join(base: str, key: str) -> str:
    return f"{base}.{key}" if base else key


# ── Layer 1: JSON Schema ────────────────────────────────────────────────────

def _schema_violations(doc: dict[str, Any]) -> Iterator[Violation]:
    yield from schema_violations(doc, load_schema())


def schema_violations(doc: Any, schema: dict[str, Any]) -> Iterator[Violation]:
    """Check ``doc`` against any schema that uses the x-messages / x-at annotations."""
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(schema)
    for error in validator.iter_errors(doc):
        yield from _translate(error)


def _translate(error: Any) -> Iterator[Violation]:
    schema = error.schema if isinstance(error.schema, dict) else {}
    messages: dict[str, str] = schema.get("x-messages", {})
    custom = messages.get(error.validator)
    parts = list(error.absolute_path)
    base = _fmt(parts)

    # A propertyNames failure is reported at the parent table, with the
    # offending key as the instance. Point at the key instead.
    if "propertyNames" in error.absolute_schema_path:
        yield Violation(_join(base, str(error.instance)), custom or _default_message(error))
        return

    if error.validator == "additionalProperties":
        allowed = list(schema.get("properties", {}))
        extras = sorted(k for k in error.instance if k not in allowed)
        for key in extras:
            yield Violation(_join(base, key), custom or _unknown_key_message(key, base, allowed))
        return

    if error.validator == "required":
        missing = [k for k in error.validator_value if k not in error.instance]
        for key in missing:
            yield Violation(_join(base, key), custom or "is required")
        return

    if "x-at" in schema:
        base = _join(base, schema["x-at"])

    yield Violation(base, custom or _default_message(error))


def _unknown_key_message(key: str, base: str, allowed: list[str]) -> str:
    if base.endswith("needs.postgres") and key in ("database", "role"):
        return (
            "bay.toml never names an adopted database or user; they differ per "
            "environment and live in the fleet lockfile (see `bay show`)"
        )
    if base and key in TOP_LEVEL_KEYS:
        return (
            "unknown key here; a top-level key written below a [table] header "
            "belongs to that table, so move it above the first table"
        )
    close = difflib.get_close_matches(key, allowed, n=1)
    if close:
        return f"unknown key; did you mean {close[0]}?"
    return "unknown key"


def _default_message(error: Any) -> str:
    v = error.validator
    val = error.validator_value
    if v == "type":
        wanted = val if isinstance(val, list) else [val]
        words = " or ".join(_TYPE_WORDS.get(str(w), str(w)) for w in wanted)
        return f"must be {words}"
    if v == "enum":
        return "must be one of " + ", ".join(str(x) for x in val)
    if v == "const":
        return f"must be {val}"
    if v == "minimum":
        return f"must be at least {val}"
    if v == "maximum":
        return f"must be at most {val}"
    if v == "minLength":
        return "must not be empty"
    if v == "minItems":
        return f"needs at least {val} item" + ("" if val == 1 else "s")
    if v == "minProperties":
        return f"needs at least {val} key" + ("" if val == 1 else "s")
    if v == "uniqueItems":
        return "lists the same item twice"
    if v == "pattern":
        return f"does not match the expected form {val}"
    if v == "oneOf":
        return "matches none or more than one of the allowed forms"
    if v == "not":
        return "is not allowed in this combination"
    if v is None:
        return "is not allowed"
    return str(error.message).split("\n", 1)[0]


# ── Layer 2: rules that span the file ────────────────────────────────────────

def _file_rules(doc: dict[str, Any]) -> Iterator[Violation]:
    # These rules read the shapes the schema already promised; a wrong type
    # is reported by layer 1 and skipped here, never raised.
    yield from _no_templating(doc, "")
    envs = _envs(doc)
    services = _dict(doc.get("services"))
    yield from _name_rules(doc, envs, services)
    yield from _needs_rules(doc, services)
    yield from _var_collisions(doc, "")
    for svc_name, svc in services.items():
        if isinstance(svc, dict):
            yield from _var_collisions(svc, f"services.{svc_name}")
    yield from _sibling_url_collisions(doc, envs, services)
    yield from _mount_rules(doc, services)
    yield from _access_rules(doc, services)
    yield from _deploy_rules(doc, envs, services)
    yield from _domain_rules(doc, envs, services)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _envs(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in _dict(doc.get("deploy")).items() if isinstance(v, dict)}


def _no_templating(node: Any, path: str) -> Iterator[Violation]:
    if isinstance(node, str):
        if _TEMPLATE_RE.search(node):
            yield Violation(path, "templating is not allowed; write the literal value")
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _no_templating(v, _join(path, str(k)))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _no_templating(v, f"{path}[{i}]")


def _name_rules(
    doc: dict[str, Any], envs: dict[str, Any], services: dict[str, Any]
) -> Iterator[Violation]:
    name = doc.get("name")
    if isinstance(name, str):
        for env in envs:
            if name.endswith(f"-{env}"):
                yield Violation(
                    "name",
                    f"must not end in -{env}, because {env} is an environment in [deploy]",
                )
    for svc_name in services:
        for env in envs:
            if svc_name.startswith(f"{env}-"):
                yield Violation(
                    f"services.{svc_name}",
                    f"service names must not start with {env}-, because container "
                    f"names for the {env} environment would collide",
                )
        if svc_name.startswith("job-"):
            yield Violation(
                f"services.{svc_name}",
                "service names must not start with job-, because the container name "
                "would collide with a scheduled job",
            )
    yield from _volume_names(doc, envs, services)
    seen: set[str] = set()
    jobs = doc.get("jobs")
    for i, job in enumerate(jobs if isinstance(jobs, list) else []):
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            continue
        jname = job["name"]
        if jname in seen:
            yield Violation(f"jobs[{i}].name", f"another job is already named {jname}")
        elif jname in services or jname == "web":
            yield Violation(f"jobs[{i}].name", f"{jname} is already the name of a service")
        seen.add(jname)


def _volume_names(
    doc: dict[str, Any], envs: dict[str, Any], services: dict[str, Any]
) -> Iterator[Violation]:
    levels = [("", doc)] + [
        (f"services.{n}", s) for n, s in services.items() if isinstance(s, dict)
    ]
    for base, level in levels:
        mounts = level.get("mounts")
        for i, m in enumerate(mounts if isinstance(mounts, list) else []):
            vol = m.get("volume") if isinstance(m, dict) else None
            if not isinstance(vol, str):
                continue
            for env in envs:
                if vol.startswith(f"{env}-"):
                    yield Violation(
                        _join(base, f"mounts[{i}].volume"),
                        f"volume names must not start with {env}-, because volume "
                        f"names for the {env} environment would collide",
                    )


def _needs_rules(doc: dict[str, Any], services: dict[str, Any]) -> Iterator[Violation]:
    sites: list[tuple[str, Any]] = [("needs", doc["needs"])] if "needs" in doc else []
    for svc_name, svc in services.items():
        if isinstance(svc, dict) and "needs" in svc:
            sites.append((f"services.{svc_name}.needs", svc["needs"]))

    forms = {p: ("list" if isinstance(v, list) else "table") for p, v in sites
             if isinstance(v, (list, dict))}
    if len(set(forms.values())) > 1:
        first_path, first_form = next(iter(forms.items()))
        for p, form in forms.items():
            if form != first_form:
                yield Violation(
                    p,
                    f"uses the {form} form but {first_path} uses the {first_form} form; "
                    "use one form in the whole file",
                )

    name = doc.get("name")
    for p, v in sites:
        names = v if isinstance(v, list) else list(_dict(v))
        if name in names:
            yield Violation(p, "a project cannot need itself")


def _var_collisions(level: dict[str, Any], base: str) -> Iterator[Violation]:
    sources: dict[str, str] = {}
    for key in _dict(level.get("env")):
        sources.setdefault(key, "env")
    for key in dict.fromkeys(_str_list(level.get("secrets"))):
        if key in sources:
            yield Violation(_join(base, "secrets"), f"{key} is also set in {sources[key]}")
        else:
            sources[key] = "secrets"
    for key in _dict(level.get("fleet_secrets")):
        if key in sources:
            yield Violation(
                _join(base, f"fleet_secrets.{key}"), f"{key} is also set in {sources[key]}"
            )
        else:
            sources[key] = "fleet_secrets"


def sibling_var(service: str) -> str:
    """The variable a service with a port injects into its siblings: ``API_URL``."""
    return service.upper().replace("-", "_") + "_URL"


def need_vars(needs: Any) -> dict[str, str]:
    """Variable name -> the need that injects it, for one ``needs`` value.

    ``postgres`` injects ``DATABASE_URL``, ``redis`` injects ``REDIS_URL``,
    any other need ``<NAME>_URL``. An ``env`` option adds a second name.
    """
    out: dict[str, str] = {}
    names = needs if isinstance(needs, list) else list(_dict(needs))
    for need in names:
        if not isinstance(need, str):
            continue
        if need == "postgres":
            out["DATABASE_URL"] = need
        elif need == "redis":
            out["REDIS_URL"] = need
        else:
            out[need.upper().replace("-", "_") + "_URL"] = need
        alias = _dict(_dict(needs).get(need)).get("env") if isinstance(needs, dict) else None
        if isinstance(alias, str):
            out[alias] = need
    return out


def _declared_vars(levels: list[dict[str, Any]]) -> dict[str, str]:
    """Variable name -> where it is declared, across the levels one container reads."""
    out: dict[str, str] = {}
    for level in levels:
        for key in _dict(level.get("env")):
            out.setdefault(key, "env")
        for key in _str_list(level.get("secrets")):
            out.setdefault(key, "secrets")
        for key in _dict(level.get("fleet_secrets")):
            out.setdefault(key, "fleet_secrets")
        if "needs" in level:
            for key, need in need_vars(level["needs"]).items():
                out.setdefault(key, f"needs.{need}")
    return out


def _sibling_url_collisions(
    doc: dict[str, Any], envs: dict[str, dict[str, Any]], services: dict[str, Any]
) -> Iterator[Violation]:
    # Producers: every container with a port. The main container is `web`.
    main_has_port = "port" in doc or any("port" in d for d in envs.values())
    producers = (["web"] if main_has_port else []) + [
        n for n, s in services.items() if isinstance(s, dict) and "port" in s
    ]
    env_levels = [{"env": d.get("env"), "secrets": d.get("secrets")} for d in envs.values()]

    # Receivers: the main container and every service that inherits.
    receivers: list[tuple[str, list[dict[str, Any]]]] = [("web", [doc, *env_levels])]
    for n, s in services.items():
        if not isinstance(s, dict) or s.get("inherit") is False:
            continue
        receivers.append((n, [doc, *env_levels, s]))

    seen: set[tuple[str, str]] = set()
    for receiver, levels in receivers:
        declared = _declared_vars(levels)
        for producer in producers:
            if producer == receiver:
                continue
            var = sibling_var(producer)
            if var not in declared:
                continue
            where = producer if producer != "web" else receiver
            if (where, var) in seen:
                continue
            seen.add((where, var))
            yield Violation(
                f"services.{where}",
                f"Bay injects {var} for {_label(producer)}, so {_label(receiver)} must "
                f"not also set it in {declared[var]}; rename one of them",
            )


def _label(service: str) -> str:
    return "the main container" if service == "web" else f"services.{service}"


def _mount_rules(doc: dict[str, Any], services: dict[str, Any]) -> Iterator[Violation]:
    levels = [("", doc)] + [
        (f"services.{n}", s) for n, s in services.items() if isinstance(s, dict)
    ]
    for base, level in levels:
        mounts = level.get("mounts")
        if not isinstance(mounts, list):
            continue
        seen: dict[str, int] = {}
        for i, m in enumerate(mounts):
            if not isinstance(m, dict) or not isinstance(m.get("path"), str):
                continue
            path = m["path"].rstrip("/") or "/"
            if path in seen:
                yield Violation(
                    _join(base, f"mounts[{i}].path"),
                    f"mounts[{seen[path]}] already uses this path in the same container",
                )
            else:
                seen[path] = i


def _access_rules(doc: dict[str, Any], services: dict[str, Any]) -> Iterator[Violation]:
    levels: list[tuple[str, Any]] = [("access", doc.get("access"))]
    for env, d in _envs(doc).items():
        levels.append((f"deploy.{env}.access", d.get("access")))
    for n, s in services.items():
        if isinstance(s, dict):
            levels.append((f"services.{n}.access", s.get("access")))
    for base, access in levels:
        access = _dict(access)
        both = set(_str_list(access.get("open"))) & set(_str_list(access.get("locked")))
        for p in sorted(both):
            yield Violation(f"{base}.locked", f"{p} is listed in both open and locked")

    top_mode = _dict(doc.get("access")).get("mode")
    env_modes = {top_mode} | {
        _dict(d.get("access")).get("mode", top_mode) for d in _envs(doc).values()
    }
    for n, s in services.items():
        if not isinstance(s, dict):
            continue
        routed = "path" in s or "domain" in s
        svc_access = _dict(s.get("access"))
        if routed and svc_access.get("mode") == "internal":
            yield Violation(
                f"services.{n}.access.mode",
                "internal means no route, so remove path or domain, or choose another mode",
            )
        if not routed and svc_access and set(svc_access) != {"mode"}:
            yield Violation(
                f"services.{n}.access",
                "has no effect on a service with no domain or path; it is internal",
            )
        if not routed and svc_access.get("mode") in ("public", "tailnet"):
            yield Violation(
                f"services.{n}.access.mode",
                "needs a domain or path to route to; without one the service is internal",
            )
        if "path" in s and "internal" in env_modes and svc_access.get("mode") is None:
            yield Violation(
                f"services.{n}.path",
                "routes on the main domain, but access.mode internal gives the main "
                "container no route in at least one environment",
            )


def _str_list(value: Any) -> list[str]:
    return [x for x in value if isinstance(x, str)] if isinstance(value, list) else []


def _deploy_rules(
    doc: dict[str, Any], envs: dict[str, dict[str, Any]], services: dict[str, Any]
) -> Iterator[Violation]:
    top_access = _dict(doc.get("access"))
    top_mode = top_access.get("mode")
    top_has_pw = "password" in top_access
    top_builds = "image" not in doc

    for env, d in envs.items():
        base = f"deploy.{env}"
        env_access = _dict(d.get("access"))

        for svc_name, override in _dict(d.get("services")).items():
            if svc_name not in services:
                yield Violation(
                    f"{base}.services.{svc_name}",
                    f"there is no [services.{svc_name}] to override",
                )
                continue
            svc = _dict(services[svc_name])
            if "domain" in _dict(override) and "path" in svc:
                yield Violation(
                    f"{base}.services.{svc_name}.domain",
                    f"services.{svc_name} routes by path on the main domain, so it cannot "
                    "take a domain",
                )

        if "build" in d and "image" not in d and not top_builds:
            yield Violation(
                f"{base}.build",
                "build.args has no effect, because this project pulls an image instead "
                "of building",
            )

        if "password" in env_access and not top_has_pw and "health" not in doc \
                and "health" not in d:
            yield Violation(
                f"{base}.health",
                "is required when this environment sets access.password; write a path "
                "or \"none\"",
            )

        mode = env_access.get("mode", top_mode)
        if "mode" in env_access and mode in ("public", "tailnet") \
                and "port" not in doc and "port" not in d:
            yield Violation(
                f"{base}.port",
                f"is required because this environment sets access.mode {mode}",
            )

        health = d.get("health", doc.get("health"))
        if isinstance(health, str) and health.startswith("/") \
                and "port" not in doc and "port" not in d:
            yield Violation(
                f"{base}.health" if "health" in d else "health",
                "a health path needs a port to probe; set port or write \"none\"",
            )

    for svc_name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        health = svc.get("health")
        if isinstance(health, str) and health.startswith("/") and "port" not in svc:
            yield Violation(
                f"services.{svc_name}.health",
                "a health path needs a port to probe; set port or write \"none\"",
            )


def _domain_rules(
    doc: dict[str, Any], envs: dict[str, dict[str, Any]], services: dict[str, Any]
) -> Iterator[Violation]:
    top_mode = _dict(doc.get("access")).get("mode")
    seen: dict[str, tuple[str, str]] = {}
    missing_domain: list[str] = []

    def claim(domain: Any, path: str, env: str) -> Iterator[Violation]:
        if not isinstance(domain, str):
            return
        if domain not in seen:
            seen[domain] = (path, env)
            return
        first_path, first_env = seen[domain]
        if first_path == path:
            svc = path.split(".")[1]
            yield Violation(
                path,
                f"{domain} would serve both {first_env} and {env}; set "
                f"deploy.{env}.services.{svc}.domain to give {env} its own domain",
            )
        else:
            yield Violation(path, f"{domain} is already used at {first_path}")

    for env, d in envs.items():
        base = f"deploy.{env}"
        mode = _dict(d.get("access")).get("mode", top_mode)
        if "domain" in d:
            yield from claim(d["domain"], f"{base}.domain", env)
        elif mode in ("public", "tailnet"):
            missing_domain.append(env)
        aliases = d.get("aliases")
        for i, alias in enumerate(aliases if isinstance(aliases, list) else []):
            yield from claim(alias, f"{base}.aliases[{i}]", env)
        overrides = _dict(d.get("services"))
        for svc_name, svc in services.items():
            if not isinstance(svc, dict):
                continue
            override = _dict(overrides.get(svc_name))
            if "domain" in override:
                yield from claim(override["domain"], f"{base}.services.{svc_name}.domain", env)
            elif "domain" in svc:
                yield from claim(svc["domain"], f"services.{svc_name}.domain", env)

    if len(missing_domain) > 1:
        for env in missing_domain[1:]:
            yield Violation(
                f"deploy.{env}.domain",
                f"is required, because deploy.{missing_domain[0]} also has no domain and "
                "both would get the same default domain",
            )
