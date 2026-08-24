"""Render crowdsec acquis.yaml.j2 and assert:

1. File-source items render BYTE-IDENTICALLY to the pre-bay#23 template
   (backward compat — the acceptance criterion "defaults render byte-identical").
2. A `source: docker` item renders a valid CrowdSec docker datasource block
   (container_name / container_name_regexp / docker_host passthrough).
3. File and docker items coexist as separate `---` YAML documents.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, FileSystemLoader

TEMPLATE_DIR = Path(__file__).parent.parent / "roles" / "crowdsec" / "templates"
TEMPLATE_NAME = "acquis.yaml.j2"

# Frozen copy of the template as it existed before bay#23. Rendering the live
# template and this copy with identical data must produce identical bytes for
# any file-source-only input — that is the backward-compat guarantee.
OLD_TEMPLATE = """\
# CrowdSec Log Acquisition Configuration
# Managed by Ansible - do not edit manually

{% for acq in crowdsec_acquisition %}
filenames:
{% for filename in acq.filenames %}
  - {{ filename }}
{% endfor %}
labels:
{% for key, value in acq.labels.items() %}
  {{ key }}: {{ value }}
{% endfor %}
{% if not loop.last %}

---
{% endif %}
{% endfor %}
"""

# Mirrors the role's default crowdsec_acquisition (all file sources).
DEFAULT_ACQUISITION = [
    {"filenames": ["/srv/www/*/logs/access.log"], "labels": {"type": "nginx"}},
    {"filenames": ["/srv/www/*/logs/error.log"], "labels": {"type": "nginx"}},
    {
        "filenames": ["/var/log/nginx/access.log", "/var/log/nginx/error.log"],
        "labels": {"type": "nginx"},
    },
    {"filenames": ["/var/log/auth.log"], "labels": {"type": "syslog"}},
]


@pytest.fixture(scope="module")
def jinja_env() -> Environment:
    # Match Ansible's templating defaults exactly so byte-identity is meaningful.
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=False,
        keep_trailing_newline=True,
    )


def _render(jinja_env: Environment, acquisition: list) -> str:
    return jinja_env.get_template(TEMPLATE_NAME).render(crowdsec_acquisition=acquisition)


def _render_old(jinja_env: Environment, acquisition: list) -> str:
    return jinja_env.from_string(OLD_TEMPLATE).render(crowdsec_acquisition=acquisition)


@pytest.mark.parametrize(
    "acquisition",
    [
        DEFAULT_ACQUISITION,
        [{"filenames": ["/var/log/auth.log"], "labels": {"type": "syslog"}}],
        [
            {"filenames": ["/a.log", "/b.log"], "labels": {"type": "nginx", "x": "y"}},
            {"filenames": ["/c.log"], "labels": {"type": "syslog"}},
        ],
    ],
)
def test_file_sources_byte_identical_to_old_template(
    jinja_env: Environment, acquisition: list
) -> None:
    assert _render(jinja_env, acquisition) == _render_old(jinja_env, acquisition)


def test_docker_source_renders_valid_datasource(jinja_env: Environment) -> None:
    acquisition = [
        {
            "source": "docker",
            "container_name": ["storefront", "blog"],
            "labels": {"type": "bot_verify"},
        }
    ]
    rendered = _render(jinja_env, acquisition)
    parsed = yaml.safe_load(rendered)
    assert parsed["source"] == "docker"
    assert parsed["container_name"] == ["storefront", "blog"]
    assert parsed["labels"] == {"type": "bot_verify"}
    # A docker item must NOT emit filenames.
    assert "filenames" not in parsed


def test_docker_regexp_and_host_passthrough(jinja_env: Environment) -> None:
    acquisition = [
        {
            "source": "docker",
            "container_name_regexp": ["^app-"],
            "docker_host": "tcp://127.0.0.1:2375",
            "labels": {"type": "bot_verify"},
        }
    ]
    parsed = yaml.safe_load(_render(jinja_env, acquisition))
    assert parsed["container_name_regexp"] == ["^app-"]
    assert parsed["docker_host"] == "tcp://127.0.0.1:2375"
    assert "container_name" not in parsed


def test_mixed_file_and_docker_sources(jinja_env: Environment) -> None:
    acquisition = [
        {"filenames": ["/var/log/auth.log"], "labels": {"type": "syslog"}},
        {
            "source": "docker",
            "container_name": ["app"],
            "labels": {"type": "bot_verify"},
        },
    ]
    docs = list(yaml.safe_load_all(_render(jinja_env, acquisition)))
    assert len(docs) == 2
    assert docs[0]["filenames"] == ["/var/log/auth.log"]
    assert "source" not in docs[0]
    assert docs[1]["source"] == "docker"
    assert docs[1]["container_name"] == ["app"]
