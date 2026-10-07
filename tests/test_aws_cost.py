"""CG14: billing dates and independent cost/credit reporting."""

import json
import subprocess
from datetime import date

import pytest

from scripts import aws_cost


@pytest.mark.parametrize(
    "today,start,end",
    [
        (date(2026, 10, 1), "2026-10-01", "2026-10-02"),
        (date(2026, 12, 31), "2026-12-01", "2027-01-01"),
        (date(2028, 2, 29), "2028-02-01", "2028-03-01"),
    ],
)
def test_cg14_cost_period_includes_today_with_exclusive_end(today, start, end):
    args = aws_cost.cost_args(today)
    assert args[args.index("--time-period") + 1] == f"Start={start},End={end}"
    assert json.loads(args[-1])["Not"]["Dimensions"]["Values"] == ["Credit", "Refund"]


def test_cg14_cost_failure_does_not_hide_credits(monkeypatch, capsys):
    def aws(*args):
        if args[0] == "ce":
            raise subprocess.CalledProcessError(1, "aws", stderr="synthetic denied")
        return {
            "accountId": "synthetic",
            "accountPlanType": "FREE",
            "accountPlanStatus": "ACTIVE",
            "accountPlanRemainingCredits": {"amount": 200, "unit": "USD"},
        }

    monkeypatch.setattr(aws_cost, "aws", aws)
    assert aws_cost.main() == 1
    output = capsys.readouterr().out
    assert "Cost Explorer unavailable" in output
    assert "Remaining Free-plan credits: 200 USD" in output


def test_cg14_credit_failure_does_not_hide_service_totals(monkeypatch, capsys):
    def aws(*args):
        if args[0] == "freetier":
            raise subprocess.CalledProcessError(1, "aws", stderr="synthetic denied")
        return {
            "ResultsByTime": [
                {
                    "Estimated": True,
                    "Groups": [
                        {
                            "Keys": ["Synthetic service"],
                            "Metrics": {"UnblendedCost": {"Amount": n, "Unit": "USD"}},
                        }
                        for n in ("0.10", "0.20")
                    ],
                }
            ]
        }

    monkeypatch.setattr(aws_cost, "aws", aws)
    assert aws_cost.main() == 1
    output = capsys.readouterr().out
    assert "Synthetic service: 0.300000 USD" in output
    assert "Free-plan state unavailable" in output
