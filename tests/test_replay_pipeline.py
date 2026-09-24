import pytest
from test_routing import active_config, route_value
from test_workflow_replay import group_event

from k3_support.replay_research import replay_research_pipeline
from k3_support.replay_snapshot import replay_snapshot
from k3_support.workflow_replay import ReplayBoundaryError


def test_pipeline_routes_then_selects_retrieved_material_on_same_snapshot(conn, config):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    before = conn.serialize()
    seen = []

    def router(context):
        seen.append(('route', context))
        return route_value('research')

    def selector(context):
        seen.append(('research', context))
        return {'document_urls': ['https://example.com/fan'], 'confidence': .99}

    with replay_snapshot(cfg.database_path) as snapshot:
        result = replay_research_pipeline(snapshot, cfg,
            event=group_event(1, 'Pico风扇怎么调节'),
            documents=[{'title': 'Pico风扇', 'url': 'https://example.com/fan', 'content': '风扇操作说明'}],
            router=router, selector=selector)
        assert [stage for stage, _ in seen] == ['route', 'research']
        assert 'https://example.com/fan' in str(seen[1][1])
        assert result['research']['completion']['state'] == 'needs_owner_review'
        assert result['research']['completion']['reason'] == 'document_route_not_evaluated'
        assert any(row['channel'] == 'telegram' and row['action_type'] == 'owner_decision'
                   for row in result['intentions']['outbox'])
        assert result['model_invoked'] is None
        assert not result['model_quality_verified']
        assert not any(row['state'] == 'delivered' for row in result['intentions']['outbox'])
    assert conn.serialize() == before


def test_pipeline_skips_research_when_owner_decision_is_required(conn, config):
    cfg = active_config(config)
    cfg.raw['scope']['technical_chat_ids'] = ['oc_support']
    with replay_snapshot(cfg.database_path) as snapshot:
        result = replay_research_pipeline(snapshot, cfg, event=group_event(1, '交付时间能承诺吗'),
            documents=[], router=lambda _: route_value('owner_decision', requires_owner_judgment=True,
                                                       reason_codes=['requires_commitment']),
            selector=lambda _: pytest.fail('unnecessary research callback'))
        assert result['research'] is None


def test_pipeline_refuses_live_database_before_callbacks(conn, config):
    before = conn.serialize()
    with pytest.raises(ReplayBoundaryError):
        replay_research_pipeline(conn, config, event={}, documents=[],
            router=lambda _: pytest.fail('router'), selector=lambda _: pytest.fail('selector'))
    assert conn.serialize() == before


def test_web_only_document_suggestion_retains_next_action(conn, config):
    cfg = active_config(config)
    cfg.raw["operator_notifications"] = {"channel": "web"}
    cfg.raw["scope"]["technical_chat_ids"] = ["oc_support"]
    with replay_snapshot(cfg.database_path) as snapshot:
        result = replay_research_pipeline(snapshot, cfg,
            event=group_event(1, "Pico风扇怎么调节"),
            documents=[{"title": "Pico风扇", "url": "https://example.com/fan", "content": "风扇操作说明"}],
            router=lambda _: route_value("research"),
            selector=lambda _: {"document_urls": ["https://example.com/fan"], "confidence": .99})
        assert result["research"]["completion"]["state"] == "needs_owner_review"
        assert result["research"]["completion"]["outbox_id"] is None
        assert snapshot.execute("SELECT next_action FROM cases").fetchone()[0] == "Owner review of unevaluated document route"
        assert not any(row["action_type"] == "owner_decision" for row in result["intentions"]["outbox"])
