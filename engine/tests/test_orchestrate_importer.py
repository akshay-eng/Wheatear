"""Tests for the Orchestrate → IR importer.

Covers both native Orchestrate YAML format (apiVersion/metadata/spec)
and Wheatear's own exporter format (spec_version/flat structure).
"""

import pytest
import yaml

from wheatear.connectors.orchestrate.importer import detect_format, import_agent


# ---------------------------------------------------------------------------
# Fixtures — native Orchestrate export format
# ---------------------------------------------------------------------------

NATIVE_YAML = """\
apiVersion: watsonx-orchestrate/v1alpha1
kind: NativeAgent
metadata:
  name: support-bot
spec:
  description: Customer support agent
  instructions: |
    You are a helpful support agent. Answer questions about orders and returns.
  llm: watsonx/meta-llama/llama-3-3-70b-instruct
  style: default
  collaborators: []
  tools:
    - name: lookup-order
    - name: process-return
  knowledge:
    - name: product-faqs
"""

# ---------------------------------------------------------------------------
# Fixtures — Wheatear exporter format
# ---------------------------------------------------------------------------

WHEATEAR_YAML = """\
spec_version: v1
kind: native
name: hr-assistant
instructions: You are an HR assistant. Help employees with benefits and policies.
llm: watsonx/meta-llama/llama-3-3-70b-instruct
style: default
collaborators: []
tools:
  - benefits-lookup
  - policy-search
knowledge_base:
  - hr-policies-kb
"""


# ---------------------------------------------------------------------------
# detect_format
# ---------------------------------------------------------------------------

def test_detect_format_native(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(NATIVE_YAML)
    assert detect_format(f) == "orchestrate"


def test_detect_format_wheatear(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(WHEATEAR_YAML)
    assert detect_format(f) == "orchestrate"


def test_detect_format_directory_with_agent_yaml(tmp_path):
    (tmp_path / "agent.yaml").write_text(NATIVE_YAML)
    assert detect_format(tmp_path) == "orchestrate"


def test_detect_format_unknown_returns_none(tmp_path):
    f = tmp_path / "random.yaml"
    f.write_text("hello: world\n")
    assert detect_format(f) is None


def test_detect_format_missing_file_returns_none(tmp_path):
    assert detect_format(tmp_path / "does-not-exist.yaml") is None


# ---------------------------------------------------------------------------
# import_agent — native format
# ---------------------------------------------------------------------------

def test_import_native_agent_name(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(NATIVE_YAML)
    result = import_agent(f)
    assert result.agent.name == "support-bot"


def test_import_native_agent_source_platform(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(NATIVE_YAML)
    result = import_agent(f)
    assert result.agent.source_platform == "orchestrate"


def test_import_native_agent_existing_instructions(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(NATIVE_YAML)
    result = import_agent(f)
    assert "support agent" in (result.agent.existing_instructions or "")


def test_import_native_tools_as_raw_refs(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(NATIVE_YAML)
    result = import_agent(f)
    assert set(result.raw_tool_refs) == {"lookup-order", "process-return"}


def test_import_native_knowledge_as_raw_refs(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(NATIVE_YAML)
    result = import_agent(f)
    assert len(result.raw_knowledge_refs) == 1
    assert result.raw_knowledge_refs[0].name == "product-faqs"


def test_import_native_llm_recorded_as_model_hint(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(NATIVE_YAML)
    result = import_agent(f)
    # The source LLM is preserved as a real IR field (model_hint), not just a note,
    # so the exporter's model tiering can use it.
    assert "llama" in (result.agent.model_hint or "").lower()


# ---------------------------------------------------------------------------
# import_agent — Wheatear format
# ---------------------------------------------------------------------------

def test_import_wheatear_agent_name(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(WHEATEAR_YAML)
    result = import_agent(f)
    assert result.agent.name == "hr-assistant"


def test_import_wheatear_existing_instructions(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(WHEATEAR_YAML)
    result = import_agent(f)
    assert "HR assistant" in (result.agent.existing_instructions or "")


def test_import_wheatear_tools(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(WHEATEAR_YAML)
    result = import_agent(f)
    assert set(result.raw_tool_refs) == {"benefits-lookup", "policy-search"}


def test_import_wheatear_knowledge_base(tmp_path):
    f = tmp_path / "agent.yaml"
    f.write_text(WHEATEAR_YAML)
    result = import_agent(f)
    assert len(result.raw_knowledge_refs) == 1
    assert result.raw_knowledge_refs[0].name == "hr-policies-kb"


# ---------------------------------------------------------------------------
# import_agent — directory layout
# ---------------------------------------------------------------------------

def test_import_from_directory(tmp_path):
    (tmp_path / "agent.yaml").write_text(WHEATEAR_YAML)
    result = import_agent(tmp_path)
    assert result.agent.name == "hr-assistant"


def test_import_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        import_agent(tmp_path / "nonexistent.yaml")


def test_import_empty_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        import_agent(tmp_path)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_import_agent_with_no_tools(tmp_path):
    data = {
        "apiVersion": "watsonx-orchestrate/v1alpha1",
        "kind": "NativeAgent",
        "metadata": {"name": "simple-agent"},
        "spec": {"instructions": "Be helpful.", "tools": [], "knowledge": []},
    }
    f = tmp_path / "agent.yaml"
    f.write_text(yaml.dump(data))
    result = import_agent(f)
    assert result.raw_tool_refs == []
    assert result.raw_knowledge_refs == []


def test_import_agent_description_used_as_fallback_instructions(tmp_path):
    data = {
        "apiVersion": "watsonx-orchestrate/v1alpha1",
        "kind": "NativeAgent",
        "metadata": {"name": "desc-only"},
        "spec": {"description": "A description-only agent", "tools": []},
    }
    f = tmp_path / "agent.yaml"
    f.write_text(yaml.dump(data))
    result = import_agent(f)
    assert "description-only" in (result.agent.existing_instructions or "")
