from __future__ import annotations

import copy
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".agents" / "skills" / "taobao-price-research"
sys.path.insert(0, str(SKILL / "scripts"))

from prepare_shortlist import build_shortlist  # noqa: E402
from rank_offers import ranked_output  # noqa: E402
from runtime_lib import read_json, validate_schema  # noqa: E402
from validate_final import validate as validate_final  # noqa: E402
from validate_inspection import validate as validate_inspection  # noqa: E402
from validate_search_results import validate as validate_search_results  # noqa: E402


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat()


def sample_plan() -> dict:
    return {
        "request_text": "搜索淘宝全新 RTX 5070 Ti 16GB 显卡 找出最便宜可行商品",
        "product": {
            "canonical_query": "RTX 5070 Ti 16GB 显卡",
            "brand": "NVIDIA",
            "model": "RTX 5070 Ti",
            "core_specification": "16GB 全新独立显卡",
            "condition": "new",
            "required_terms": ["5070 Ti", "16GB"],
            "excluded_terms": ["定金", "预售", "二手", "散热器", "支架", "空盒"],
        },
        "purchase_context": {
            "destination_region": "上海",
            "membership": "无已知会员权益",
        },
        "constraints": {
            "maximum_budget_cny": "unknown",
            "allowed_sale_types": ["standard"],
            "require_in_stock": True,
        },
        "search": {
            "queries": [
                "RTX 5070 Ti 16GB 显卡",
                "5070Ti 16G 全新显卡",
                "GeForce RTX5070Ti 现货",
            ],
            "pages_per_query": 1,
            "candidate_limit": 45,
            "detail_limit": 8,
            "review_limit": 3,
            "pacing": {
                "maximum_parallel_pages": 1,
                "minimum_action_interval_seconds": 3,
                "risk_event_retries": 0,
                "reuse_observation_cache_seconds": 900,
            },
        },
        "assumptions": ["未提供会员权益 按公开价处理"],
    }


def candidate(
    identifier: str,
    title: str,
    price: str,
    item_id: str,
    query: str = "RTX 5070 Ti 16GB 显卡",
) -> dict:
    return {
        "candidate_id": identifier,
        "source_backend": "aialra-shopping-browser",
        "query": query,
        "page_number": 1,
        "title": title,
        "displayed_price_min": price,
        "displayed_price_max": price,
        "seller_name": f"店铺{identifier}",
        "sales_text": "100+人付款",
        "image_url": "https://img.alicdn.com/example.jpg",
        "url": f"https://item.taobao.com/item.htm?id={item_id}&spm=tracking",
        "retrieved_at": now_iso(),
    }


def sample_search_results() -> dict:
    plan = sample_plan()
    cards = [
        candidate("good-a", "RTX 5070 Ti 16GB 全新显卡 现货", "6599", "100001"),
        candidate("good-a-copy", "RTX 5070 Ti 16GB 全新显卡 现货", "6599", "100001"),
        candidate("bad-accessory", "RTX 5070 Ti 显卡散热器支架", "99", "100002"),
        candidate(
            "good-b",
            "GeForce RTX 5070 Ti 16GB 全新显卡",
            "6399",
            "100003",
            "5070Ti 16G 全新显卡",
        ),
    ]
    return {
        "plan": plan,
        "coverage": {
            "queries_executed": plan["search"]["queries"][:2],
            "source_backend": "aialra-shopping-browser",
            "pages_read": 2,
            "cards_seen": 4,
            "blocked_reasons": [],
        },
        "candidates": cards,
    }


def offer(
    offer_id: str,
    price: str,
    *,
    shop_type: str,
    rating: str,
    return_policy: str = "supported",
    warranty: str = "三年保修",
    sale_type: str = "standard",
) -> dict:
    return {
        "offer_id": offer_id,
        "search_backends": ["aialra-shopping-browser"],
        "detail_backend": "aialra-shopping-browser",
        "title": f"RTX 5070 Ti 16GB 全新显卡 {offer_id}",
        "url": f"https://item.taobao.com/item.htm?id={offer_id.removeprefix('o')}",
        "image_urls": ["https://img.alicdn.com/example.jpg"],
        "selected_sku": "RTX 5070 Ti 16GB 全新",
        "match_confidence": "high",
        "match_reasons": ["型号与显存一致"],
        "condition": "new",
        "sale_type": sale_type,
        "stock": "in-stock",
        "price_cny": price,
        "shipping_cny": "0",
        "fees_cny": "0",
        "coupon_cny": "0",
        "coupon_verified": False,
        "cashback_cny": "unknown",
        "price_scope": "public",
        "seller": {
            "name": f"店铺{offer_id}",
            "shop_type": shop_type,
            "rating": rating,
        },
        "reviews": {
            "inspected": True,
            "total": "1000",
            "positive_rate": "99%",
            "negative_themes": [],
        },
        "return_policy": return_policy,
        "warranty": warranty,
        "evidence_level": "A",
        "retrieved_at": now_iso(),
        "notes": [],
    }


def sample_inspection() -> dict:
    plan = sample_plan()
    offers = [
        offer("o100001", "6599", shop_type="official-flagship", rating="4.9"),
        offer("o100003", "6399", shop_type="marketplace", rating="4.8"),
        offer(
            "o100004",
            "2000",
            shop_type="unknown",
            rating="unknown",
            return_policy="unknown",
            warranty="unknown",
        ),
        offer(
            "o100005",
            "99",
            shop_type="marketplace",
            rating="4.9",
            sale_type="accessory",
        ),
    ]
    return {
        "plan": plan,
        "coverage": {
            "queries_executed": plan["search"]["queries"][:2],
            "source_backend": "aialra-shopping-browser",
            "pages_read": 2,
            "cards_seen": 20,
            "shortlisted": 4,
            "details_attempted": 4,
            "details_verified": 4,
            "reviews_inspected": 4,
            "blocked_reasons": [],
            "failed_urls": [],
        },
        "offers": offers,
    }


class TaobaoDomainTests(unittest.TestCase):
    def test_search_batch_preserves_plan_and_budgets(self) -> None:
        payload = sample_search_results()
        self.assertEqual([], validate_search_results(payload["plan"], payload))
        corrupted = copy.deepcopy(payload)
        corrupted["plan"]["search"]["queries"].append("未授权搜索词")
        self.assertTrue(validate_search_results(payload["plan"], corrupted))

    def test_shortlist_deduplicates_filters_and_removes_tracking(self) -> None:
        result = build_shortlist(sample_search_results())
        self.assertEqual(1, result["filter_summary"]["duplicates_removed"])
        self.assertEqual(1, result["filter_summary"]["obvious_mismatches_removed"])
        self.assertEqual(2, result["filter_summary"]["selected_for_detail"])
        self.assertEqual(
            "https://item.taobao.com/item.htm?id=100003",
            result["shortlist"][0]["url"],
        )
        self.assertEqual(
            ["aialra-shopping-browser"],
            result["shortlist"][0]["source_backends"],
        )

    def test_inspection_requires_direct_fresh_evidence(self) -> None:
        shortlist = build_shortlist(sample_search_results())
        inspection = sample_inspection()
        inspection["plan"] = shortlist["plan"]
        inspection["offers"] = inspection["offers"][:2]
        inspection["coverage"]["details_attempted"] = 2
        inspection["coverage"]["details_verified"] = 2
        inspection["coverage"]["reviews_inspected"] = 2
        inspection["offers"][0]["offer_id"] = shortlist["shortlist"][0]["candidate_id"]
        inspection["offers"][1]["offer_id"] = shortlist["shortlist"][1]["candidate_id"]
        inspection["offers"][0]["url"] = shortlist["shortlist"][0]["url"]
        inspection["offers"][1]["url"] = shortlist["shortlist"][1]["url"]
        self.assertEqual([], validate_inspection(shortlist, inspection))
        inspection["offers"][0]["evidence_level"] = "B"
        schema = read_json(SKILL / "schemas" / "inspection.schema.json")
        self.assertTrue(validate_schema(inspection, schema))

    def test_rank_selects_cheapest_viable_offer_not_cheapest_card(self) -> None:
        result = ranked_output(sample_inspection())
        self.assertEqual("lowest-verified-total", result["recommendation"]["decision_type"])
        self.assertEqual("o100003", result["recommendation"]["winner_id"])
        by_id = {item["offer_id"]: item for item in result["offers"]}
        self.assertFalse(by_id["o100004"]["eligible"])
        self.assertEqual("high", by_id["o100004"]["risk_level"])
        self.assertFalse(by_id["o100005"]["eligible"])
        self.assertEqual([], validate_final(result))

    def test_unverified_coupon_is_not_subtracted(self) -> None:
        inspection = sample_inspection()
        inspection["offers"] = [inspection["offers"][0]]
        inspection["offers"][0]["coupon_cny"] = "500"
        inspection["offers"][0]["coupon_verified"] = False
        inspection["coverage"]["shortlisted"] = 1
        inspection["coverage"]["details_attempted"] = 1
        inspection["coverage"]["details_verified"] = 1
        inspection["coverage"]["reviews_inspected"] = 1
        result = ranked_output(inspection)
        self.assertEqual("6599.00", result["offers"][0]["known_total_cny"])

    def test_final_validator_rejects_false_winner(self) -> None:
        result = ranked_output(sample_inspection())
        result["recommendation"]["winner_id"] = "o100001"
        self.assertTrue(validate_final(result))

    def test_skill_trigger_and_side_effect_boundaries_are_explicit(self) -> None:
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        workflow = read_json(SKILL / "workflow.yaml")
        self.assertIn("淘宝查价", skill_text)
        self.assertIn("不用于自动下单", skill_text)
        effects = {node["side_effect"] for node in workflow["execution"]["graph"]["nodes"]}
        self.assertLessEqual(effects, {"none", "read"})
        self.assertTrue(workflow["definition"]["configured"])
        browser_nodes = [
            node
            for node in workflow["execution"]["graph"]["nodes"]
            if node["executor"] == "mcp"
        ]
        self.assertTrue(browser_nodes)
        for node in browser_nodes:
            arguments = node["action"]["arguments"]
            self.assertEqual(1, arguments["parallelism_hint"])
            self.assertGreaterEqual(arguments["minimum_action_interval_seconds"], 3)
            self.assertEqual(0, arguments["risk_event_retries"])
            self.assertGreaterEqual(arguments["reuse_observation_cache_seconds"], 900)
            self.assertEqual(0, node["max_retries"])
            self.assertEqual(
                ["aialra-shopping-browser"],
                arguments["source_routing"]["provider_order"],
            )


class RunnerEndToEndTests(unittest.TestCase):
    def run_runner(self, *arguments: str, expect: int = 0) -> dict:
        command = [
            sys.executable,
            str(SKILL / "scripts" / "runner.py"),
            *arguments,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(expect, completed.returncode, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def write_json(self, directory: Path, name: str, value: dict) -> Path:
        path = directory / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_runner_completes_external_and_script_nodes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            start = self.run_runner(
                "start",
                "--input",
                str(
                    self.write_json(
                        directory,
                        "input.json",
                        {
                            "request_text": "搜索淘宝全新 RTX 5070 Ti 16GB 显卡最低可行价格",
                            "destination_region": "上海",
                        },
                    )
                ),
            )
            state_id = start["state_id"]
            directive = self.run_runner("advance", "--state-id", state_id)
            self.assertEqual("interpret-request", directive["node"]["id"])
            directive = self.run_runner(
                "submit",
                "--state-id",
                state_id,
                "--node-id",
                "interpret-request",
                "--output",
                str(self.write_json(directory, "plan.json", sample_plan())),
            )
            self.assertEqual("collect-candidates", directive["node"]["id"])
            directive = self.run_runner("advance", "--state-id", state_id)
            self.assertEqual("waiting-external", directive["status"])
            directive = self.run_runner(
                "submit",
                "--state-id",
                state_id,
                "--node-id",
                "collect-candidates",
                "--output",
                str(self.write_json(directory, "search.json", sample_search_results())),
            )
            self.assertEqual("prepare-shortlist", directive["node"]["id"])
            directive = self.run_runner("advance", "--state-id", state_id)
            self.assertEqual("inspect-details", directive["node"]["id"])
            shortlist_state = self.run_runner("status", "--state-id", state_id)
            shortlist = shortlist_state["current_input"]
            inspection = sample_inspection()
            inspection["plan"] = shortlist["plan"]
            inspection["offers"] = inspection["offers"][:2]
            inspection["coverage"]["shortlisted"] = len(shortlist["shortlist"])
            inspection["coverage"]["details_attempted"] = 2
            inspection["coverage"]["details_verified"] = 2
            inspection["coverage"]["reviews_inspected"] = 2
            for index, item in enumerate(inspection["offers"]):
                item["offer_id"] = shortlist["shortlist"][index]["candidate_id"]
                item["url"] = shortlist["shortlist"][index]["url"]
            directive = self.run_runner(
                "submit",
                "--state-id",
                state_id,
                "--node-id",
                "inspect-details",
                "--output",
                str(self.write_json(directory, "inspection.json", inspection)),
            )
            self.assertEqual("rank-offers", directive["node"]["id"])
            completed = self.run_runner("advance", "--state-id", state_id)
            self.assertEqual("completed", completed["status"])
            self.assertEqual(
                "lowest-verified-total",
                completed["final_output"]["recommendation"]["decision_type"],
            )

    def test_runner_pauses_for_user_login_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            start = self.run_runner(
                "start",
                "--input",
                str(
                    self.write_json(
                        directory,
                        "input.json",
                        {"request_text": "搜索淘宝 RTX 5070 Ti 16GB 显卡价格"},
                    )
                ),
            )
            state_id = start["state_id"]
            waiting = self.run_runner("advance", "--state-id", state_id)
            self.assertIn(
                "policy-blocked",
                waiting["failure_submission"]["allowed_kinds"],
            )
            self.run_runner(
                "submit",
                "--state-id",
                state_id,
                "--node-id",
                "interpret-request",
                "--output",
                str(self.write_json(directory, "plan.json", sample_plan())),
            )
            self.run_runner("advance", "--state-id", state_id)
            paused = self.run_runner(
                "fail",
                "--state-id",
                state_id,
                "--node-id",
                "collect-candidates",
                "--kind",
                "user-required",
                "--message",
                "需要用户亲自登录",
            )
            self.assertEqual("waiting-user", paused["status"])
            resumed = self.run_runner("resume", "--state-id", state_id)
            self.assertEqual("running", resumed["status"])
            waiting = self.run_runner("advance", "--state-id", state_id)
            self.assertEqual("waiting-external", waiting["status"])

    def test_runner_stops_on_host_policy_block(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            start = self.run_runner(
                "start",
                "--input",
                str(
                    self.write_json(
                        directory,
                        "input.json",
                        {"request_text": "搜索淘宝 RTX 5070 Ti 16GB 显卡价格"},
                    )
                ),
            )
            state_id = start["state_id"]
            self.run_runner("advance", "--state-id", state_id)
            self.run_runner(
                "submit",
                "--state-id",
                state_id,
                "--node-id",
                "interpret-request",
                "--output",
                str(self.write_json(directory, "plan.json", sample_plan())),
            )
            self.run_runner("advance", "--state-id", state_id)
            failed = self.run_runner(
                "fail",
                "--state-id",
                state_id,
                "--node-id",
                "collect-candidates",
                "--kind",
                "policy-blocked",
                "--message",
                "宿主策略禁止淘宝页面访问",
                expect=1,
            )
            self.assertEqual("failed", failed["status"])
            self.assertEqual("policy-blocked", failed["error"]["kind"])


if __name__ == "__main__":
    unittest.main()
