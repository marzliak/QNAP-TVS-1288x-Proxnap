"""Offline namespace/service contracts using selected AST and inert effects only."""
from __future__ import annotations

import argparse
import ast
import builtins
import dataclasses
import __future__
import json
import math
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call
import urllib.error
import urllib.parse
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UNITS = (
    "proxnap-lcd", "proxnap-fan-control", "proxnap-status-led",
    "proxnap-panel-breath", "proxnap-copy-button", "proxnap-power-button",
    "qnap8528-load-module", "pveproxy", "pvedaemon", "pvestatd", "zfs-zed", "smartd",
)
DEFINITIONS = {
    "Settings", "env_int", "load_settings", "validate_measurement_prefix",
    "transform_measurement_prefix", "validate_services", "parse_services",
    "field_value", "esc_tag", "esc_measurement", "esc_string", "line", "emit",
    "collect_services", "write_influx", "collect_all", "main", "disk_slot_map",
}


def refuse(*args, **kwargs):
    raise AssertionError("unmocked production effect refused")


@pytest.fixture
def metrics(monkeypatch):
    assert os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"
    scratch = Path(os.environ["TMPDIR"]).resolve()
    assert str(scratch) not in {"/root", "/tmp", "/var/tmp"}
    source = (ROOT / "src/proxnap/metrics.py").read_text()
    tree = ast.parse(source)
    nodes = [node for node in tree.body if
             isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in DEFINITIONS or
             isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                                                  t.id == "DEFAULT_SERVICES" for t in node.targets)]
    assert {node.name for node in nodes if hasattr(node, "name")} == DEFINITIONS
    module = ModuleType("fixture_metrics_compatibility")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    network = Mock(side_effect=refuse)
    process = Mock(side_effect=refuse)
    namespace = module.__dict__
    namespace.update(
        __builtins__=dict(vars(builtins), open=refuse, __import__=refuse),
        dataclass=dataclasses.dataclass, field=dataclasses.field,
        socket=SimpleNamespace(gethostname=lambda: "fixture"),
        os=SimpleNamespace(environ={}), re=re, math=math, json=json, argparse=argparse,
        time=SimpleNamespace(time=lambda: 1000, monotonic=lambda: 1000, sleep=Mock(side_effect=refuse)),
        subprocess=SimpleNamespace(call=process, DEVNULL=-3),
        urllib=SimpleNamespace(parse=urllib.parse, error=urllib.error,
                               request=SimpleNamespace(Request=urllib.request.Request, urlopen=network)),
        collect_status=Mock(side_effect=lambda *args: None),
    )
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<metrics-selected>", "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    module.network = network
    module.process = process
    module.measurements = sorted({node.args[1].value for node in ast.walk(tree)
                                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                                  and node.func.id == "emit" and isinstance(node.args[1], ast.Constant)})
    return module


def points(metrics):
    return [metrics.line(name, {"host": "proxnap_host", "sensor": "a b,c=d\\e"},
                         {"text": 'proxnap_value "quote" \\ backslash', "n": 7,
                          "ratio": 1.25, "enabled": True}, 1000)
            for name in metrics.measurements]


def test_settings_defaults_and_template_preserve_generic_contract(metrics):
    settings = metrics.load_settings()
    assert settings.measurement_prefix == "proxnap"
    assert settings.services == DEFAULT_UNITS == metrics.DEFAULT_SERVICES
    assert (settings.interval_fast, settings.interval_medium, settings.interval_qnap,
            settings.interval_slow, settings.timeout) == (10, 30, 120, 300, 8)
    assert (settings.influx_url, settings.influx_org, settings.influx_bucket,
            settings.influx_token, settings.precision) == ("", "proxnap", "proxnap", "", "s")
    template = dict(row.split("=", 1) for row in (ROOT / "configs/metrics.env.example").read_text().splitlines()
                    if row and not row.startswith("#"))
    assert template["PROXNAP_METRICS_MEASUREMENT_PREFIX"] == "proxnap"
    assert metrics.parse_services(template["PROXNAP_METRICS_SERVICES"]) == DEFAULT_UNITS
    assert metrics.disk_slot_map() == {}


def test_load_settings_overrides_only_selected_contracts(metrics):
    metrics.os.environ.update(PROXNAP_METRICS_MEASUREMENT_PREFIX="compat",
                              PROXNAP_METRICS_SERVICES=" alpha.service , worker@east.service ",
                              PROXNAP_METRICS_HOST="fixture-host",
                              PROXNAP_INFLUX_URL="https://inert.invalid/",
                              PROXNAP_INFLUX_ORG="fixture-org",
                              PROXNAP_INFLUX_BUCKET="fixture-bucket",
                              PROXNAP_INFLUX_TOKEN="fixture-only",
                              PROXNAP_INFLUX_PRECISION="ms",
                              PROXNAP_METRICS_INTERVAL_FAST="11",
                              PROXNAP_METRICS_INTERVAL_MEDIUM="31",
                              PROXNAP_METRICS_INTERVAL_QNAP="121",
                              PROXNAP_METRICS_INTERVAL_SLOW="301",
                              PROXNAP_METRICS_TIMEOUT="9",
                              PROXNAP_METRICS_SLOT_MAP='{"fixture-disk":"bay-a"}')
    settings = metrics.load_settings()
    assert settings.measurement_prefix == "compat"
    assert settings.services == ("alpha.service", "worker@east.service")
    assert settings.host == "fixture-host"
    assert (settings.influx_url, settings.influx_org, settings.influx_bucket,
            settings.influx_token, settings.precision) == (
                "https://inert.invalid", "fixture-org", "fixture-bucket", "fixture-only", "ms")
    assert (settings.interval_fast, settings.interval_medium, settings.interval_qnap,
            settings.interval_slow, settings.timeout) == (11, 31, 121, 301, 9)
    assert metrics.disk_slot_map() == {"fixture-disk": "bay-a"}


@pytest.mark.parametrize("prefix", ["proxnap", "compat", "Site_2", "a"])
def test_all_measurements_keep_suffix_tags_fields_timestamp_and_escaping(metrics, prefix):
    original = points(metrics)
    assert original
    for row in original:
        expected = prefix + row[len("proxnap"):]
        actual = metrics.transform_measurement_prefix(row, prefix)
        assert actual.encode() == expected.encode()
        assert actual[len(prefix):].encode() == row[len("proxnap"):].encode()
        assert "host=proxnap_host" in actual and 'text="proxnap_value' in actual
        assert actual.endswith(" 1000")
        if prefix == "proxnap":
            assert actual.encode() == row.encode()
    # Only the leading namespace changes, not the repeated substring in the suffix.
    assert metrics.transform_measurement_prefix("proxnap_proxnap_status value=9i 1000", prefix) == (
        prefix + "_proxnap_status value=9i 1000")


@pytest.mark.parametrize("measurement", ["other", "other_proxnap_cpu", "proxnap", "proxnapX_cpu"])
def test_nonmatching_measurements_and_tag_field_prefixes_are_untouched(metrics, measurement):
    row = metrics.line(measurement, {"proxnap_tag": "proxnap_value"}, {"proxnap_field": "proxnap_value"}, 1000)
    assert metrics.transform_measurement_prefix(row, "compat") == row
    # An escaped suffix is kept verbatim, not decoded and re-encoded.
    row = metrics.line("proxnap_a b,c\\d", {"host": "fixture"}, {"n": 1}, 1000)
    assert metrics.transform_measurement_prefix(row, "compat") == "compat" + row[len("proxnap"):]


@pytest.mark.parametrize("prefix", ["", None, 42, True, " compat", "compat ", "1compat", "_compat",
                                   "a-b", "a.b", "a,b", "a=b", "a b", "a\\b", "a\nb", "a\rb",
                                   "a\tb", "á", "a\x00", "a\u2028b"])
def test_invalid_prefixes_fail_in_helper_settings_and_environment(metrics, prefix):
    with pytest.raises(ValueError):
        metrics.transform_measurement_prefix("proxnap_cpu n=1i 1000", prefix)
    with pytest.raises(ValueError):
        metrics.Settings(measurement_prefix=prefix)
    metrics.os.environ["PROXNAP_METRICS_MEASUREMENT_PREFIX"] = prefix
    with pytest.raises(ValueError):
        metrics.load_settings()
    metrics.process.assert_not_called()
    metrics.network.assert_not_called()


@pytest.mark.parametrize("raw", ["", " ", ",", "alpha,", ",alpha", "alpha,,beta", "alpha,alpha",
                                "--all", "-unit", "/etc/unit.service", "../unit", "a b", "a;b", "a&b",
                                "a|b", "$(unit)", "a=b", "a*b", "a?b", "a[b]", "a\\x20b", "a\x00b",
                                "a\nb", "a\rb", "a\tb", "alpha,\nbeta", "alpha, beta\r", "únit",
                                "a@@b", "a@", "alpha.", "alpha.invalid", "alpha.service@east",
                                "a" * 255, "a" * 256, None, 42])
def test_invalid_service_csv_is_rejected_before_effects(metrics, raw):
    with pytest.raises(ValueError):
        metrics.parse_services(raw)
    metrics.os.environ["PROXNAP_METRICS_SERVICES"] = raw
    with pytest.raises(ValueError):
        metrics.load_settings()
    metrics.process.assert_not_called()
    metrics.network.assert_not_called()


def test_service_name_subset_preserves_order_and_spelling(metrics):
    units = ("alpha", "beta.service", "worker@east.service", "worker@.service",
             "dbus-org.example.service", "periodic.timer", "a_b:c-d",
             "a" * (255 - len(".service")), "b" * (255 - len(".service")) + ".service")
    assert metrics.parse_services(" , ".join(units)) == units
    for invalid in ((), ["alpha"], ("alpha", None), ("alpha", "alpha"), ("--all",)):
        with pytest.raises(ValueError):
            metrics.Settings(services=invalid)


def test_configured_disabled_and_inactive_units_are_data_not_filters(metrics):
    settings = metrics.Settings(services=("alpha.service", "worker@east.service", "gamma"))
    metrics.process.side_effect = [0, 1, 3, 1, 0, 0]
    rows = []
    metrics.collect_services(rows, settings, 1000)
    expected = [("alpha.service", 1, 0), ("worker@east.service", 0, 0), ("gamma", 1, 1)]
    assert rows == [f"proxnap_service,host=fixture,service={unit} active={active}i,enabled={enabled}i 1000"
                    for unit, active, enabled in expected]
    assert metrics.process.call_args_list == [
        call(["systemctl", operation, "--quiet", unit], stdout=-3, stderr=-3)
        for unit, _, _ in expected for operation in ("is-active", "is-enabled")]
    metrics.collect_status.assert_called_once_with(rows, settings, 1000)
    metrics.network.assert_not_called()


def test_invalid_mutated_services_refused_before_any_command(metrics):
    settings = metrics.Settings()
    settings.services = ("--all",)
    with pytest.raises(ValueError):
        metrics.collect_services([], settings, 1000)
    metrics.process.assert_not_called()
    metrics.collect_status.assert_not_called()


@pytest.mark.parametrize("prefix", ["proxnap", "compat"])
def test_writer_dry_run_and_http_payload_match_byte_for_byte(metrics, prefix, capsys):
    original = points(metrics)
    saved = list(original)
    expected = "\n".join(metrics.transform_measurement_prefix(row, prefix) for row in original) + "\n"
    settings = metrics.Settings(measurement_prefix=prefix, dry_run=True)
    metrics.write_influx(settings, original)
    assert capsys.readouterr().out.encode() == expected.encode()
    metrics.network.assert_not_called()
    settings.dry_run = False
    settings.influx_url = "https://inert.invalid"
    settings.influx_org = "fixture org"
    settings.influx_bucket = "fixture/bucket"
    settings.influx_token = "fixture-only"
    settings.precision = "s"
    metrics.network.side_effect = None
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    metrics.network.return_value = response
    metrics.write_influx(settings, original)
    metrics.network.assert_called_once()
    request = metrics.network.call_args.args[0]
    assert request.data == expected.encode()
    assert request.full_url == "https://inert.invalid/api/v2/write?org=fixture+org&bucket=fixture%2Fbucket&precision=s"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Token fixture-only"
    assert request.get_header("Content-type") == "text/plain; charset=utf-8"
    assert metrics.network.call_args.kwargs == {"timeout": 8}
    response.read.assert_called_once_with()
    assert original == saved
    if prefix == "proxnap":
        assert request.data == ("\n".join(original) + "\n").encode()


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("bad", ["proxnap_cpu n=1i 1000\nother n=2i 1000", "proxnap_cpu n=1i 1000\r", None])
def test_line_injection_or_nonstring_rejects_entire_batch_before_output(metrics, dry_run, bad, capsys):
    settings = metrics.Settings(measurement_prefix="compat", dry_run=dry_run)
    with pytest.raises(ValueError):
        metrics.write_influx(settings, ["proxnap_cpu n=1i 1000", bad])
    assert capsys.readouterr().out == ""
    metrics.network.assert_not_called()


def test_line_emit_api_and_no_valid_fields_contract_are_unchanged(metrics):
    row = metrics.line("proxnap_cpu", {"z": "a b,c=d", "a": "fixture"},
                       {"n": 7, "ok": False, "text": 'a"b', "none": None, "nan": math.nan}, 1000)
    assert row == 'proxnap_cpu,a=fixture,z=a\\ b\\,c\\=d n=7i,ok=false,text="a\\"b" 1000'
    rows = []
    assert metrics.emit(rows, "proxnap_cpu", {"a": "fixture"}, {"n": 7}, 1000) is None
    assert rows == ["proxnap_cpu,a=fixture n=7i 1000"]
    assert metrics.line("proxnap_cpu", {}, {"none": None, "nan": math.nan}, 1000) is None
    metrics.emit(rows, "proxnap_cpu", {}, {"none": None}, 1000)
    assert len(rows) == 1


def test_empty_output_and_missing_credentials_contract_remain(metrics, capsys):
    settings = metrics.Settings()
    metrics.write_influx(settings, [])
    assert capsys.readouterr().out == ""
    settings.dry_run = True
    metrics.write_influx(settings, [])
    assert capsys.readouterr().out == "\n"
    settings.dry_run = False
    with pytest.raises(RuntimeError, match="PROXNAP_INFLUX_URL and PROXNAP_INFLUX_TOKEN are required"):
        metrics.write_influx(settings, ["proxnap_cpu n=1i 1000"])
    metrics.network.assert_not_called()


def test_main_once_dry_run_uses_configured_prefix_for_every_collection_tier(metrics, monkeypatch, capsys):
    metrics.os.environ["PROXNAP_METRICS_MEASUREMENT_PREFIX"] = "compat"
    metrics.block_identity = Mock(return_value={})
    collected = []
    for name in ("collect_hwmon", "collect_cpu", "collect_mem", "collect_net", "collect_diskio",
                 "collect_services", "collect_zfs", "collect_pve", "collect_smart"):
        def fixture_collect(rows, settings, ts, *args, _name=name, **kwargs):
            collected.append((_name, kwargs))
            metrics.emit(rows, "proxnap_" + _name, {"host": settings.host}, {"n": 1}, ts)
        setattr(metrics, name, fixture_collect)
    monkeypatch.setattr(sys, "argv", ["proxnap-metrics", "--once", "--dry-run"])
    assert metrics.main() == 0
    output = capsys.readouterr().out.splitlines()
    assert len(collected) == 9
    assert all(row.startswith("compat_collect_") and row.endswith(" n=1i 1000") for row in output[:-1])
    assert output[-1] == "wrote 9 lines tier=slow qnap_ec=1"
    assert collected[0] == ("collect_hwmon", {"include_qnap_ec": True})
    metrics.time.sleep.assert_not_called()
    metrics.process.assert_not_called()
    metrics.network.assert_not_called()


def test_fixture_refuses_unmocked_network_commands_and_file_io(metrics):
    with pytest.raises(AssertionError):
        metrics.process(["systemctl", "restart", "fixture"])
    with pytest.raises(AssertionError):
        metrics.network("https://inert.invalid")
    with pytest.raises(AssertionError):
        metrics.__dict__["__builtins__"]["open"]("/sys/class/hwmon")
