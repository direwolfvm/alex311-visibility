"""Crawl diff: what changed between two read-only wizard walks."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "rules_diff", Path(__file__).resolve().parents[1] / "spike/rules_diff.py")
rules_diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules_diff)

diff, rule_of = rules_diff.diff, rules_diff.rule_of


def crawl(*categories):
    return {"generated": "test", "categories": list(categories)}


def category(code="TESNOISE", name="Noise Issues", questions=(), continue_end=True):
    return {"service_code": code, "service_name": name, "questions": list(questions),
            "continue_enabled_at_end": continue_end}


def q(order=1, text="What is the source?", options=("Other", "Music"),
      probes=None, required=True, input="radio"):
    return {"order": order, "question": text, "required": required, "input": input,
            "options": list(options), "probes": probes or {o: {} for o in options}}


def kinds(changes):
    return sorted(c.split()[0] for c in changes)


def test_identical_crawls_are_quiet():
    a = crawl(category(questions=[q()]))
    assert diff(a, crawl(category(questions=[q()]))) == []


def test_new_and_dropped_options():
    old = crawl(category(questions=[q(options=("Other", "Music"))]))
    new = crawl(category(questions=[q(options=("Other", "Construction"))]))
    assert kinds(diff(old, new)) == ["option_added", "option_removed"]


def test_new_question():
    old = crawl(category(questions=[q(1)]))
    new = crawl(category(questions=[q(1), q(2, text="How long?")]))
    assert kinds(diff(old, new)) == ["question_added"]


def test_required_and_widget_changes():
    old = crawl(category(questions=[q(required=True, input="radio")]))
    new = crawl(category(questions=[q(required=False, input="dropdown")]))
    assert kinds(diff(old, new)) == ["required_changed", "widget_changed"]


def test_a_relaxed_hard_stop_is_reported():
    hard = {"Music": {"hard_stop": True, "alert": ["Validation Alert Call the police."]}}
    old = crawl(category(questions=[q(probes={"Other": {}, **hard})]))
    new = crawl(category(questions=[q(probes={"Other": {}, "Music": {}})]))
    changes = diff(old, new)
    assert kinds(changes) == ["rule_changed"]
    assert "hard_stop -> none" in changes[0]


def test_a_reworded_city_message_is_reported_but_can_be_ignored():
    old = crawl(category(questions=[q(probes={
        "Other": {}, "Music": {"advisory": True, "alert": ["Validation Alert Call 703.746.4444."]}})]))
    new = crawl(category(questions=[q(probes={
        "Other": {}, "Music": {"advisory": True, "alert": ["Validation Alert Call 703.746.4311."]}})]))
    assert kinds(diff(old, new)) == ["rule_message_changed"]
    assert diff(old, new, ignore_message_text=True) == []


def test_a_service_that_stopped_completing_is_reported():
    old = crawl(category(continue_end=True, questions=[q()]))
    new = crawl(category(continue_end=False, questions=[q()]))
    assert kinds(diff(old, new)) == ["walk_status_changed"]


def test_added_and_removed_services():
    old = crawl(category("A"), category("B"))
    new = crawl(category("A"), category("C"))
    assert kinds(diff(old, new)) == ["service_added", "service_removed"]


def test_rule_of_strips_the_noise_the_wizard_always_shows():
    kind, message = rule_of({"hard_stop": True, "alert": [
        "Validation Alert Please call 703.746.4444.",
        "Validation Alert",
        "There is 1 mandatory question unanswered"]})
    assert kind == "hard_stop"
    assert message == "Please call 703.746.4444."


def test_rule_of_recognises_a_service_type_suggestion():
    kind, _ = rule_of({"suggests": "New Service Type Suggestion ... Keep Current Alley or Street"})
    assert kind == "suggestion"
