import os, tempfile, unittest
from pathlib import Path

class SafetyTests(unittest.TestCase):
    def test_render_live_switch_defaults_off(self):
        text=Path(__file__).with_name("render.yaml").read_text()
        block=text.split("- key: LIVE_TRADING",1)[1].split("- key:",1)[0]
        self.assertIn('value: "false"', block)
        self.assertIn('value: "true"', text.split("- key: EMERGENCY_STOP",1)[1].split("- key:",1)[0])
    def test_assets_have_positive_steps_and_explicit_series(self):
        import json
        data=json.loads(Path(__file__).with_name("assets.json").read_text())['assets']
        for asset, item in data.items():
            self.assertGreater(float(item['target_step']), 0, asset)
            self.assertIn('live_enabled', item, asset)
            if item['live_enabled']:
                self.assertTrue(str(item.get('kalshi_series_ticker','')).strip(), asset)
    def test_risk_gate_limits_and_fraction(self):
        import portfolio_risk as risk
        with tempfile.TemporaryDirectory() as td:
            risk.DB=str(Path(td)/"risk.sqlite3")
            now=1800000000
            a,reason=risk.reserve_entry("BTC-USD",100,400,max_per_hour=3,fraction=.25,now=now)
            self.assertEqual(reason,"reserved"); self.assertAlmostEqual(a['budget'],100)
            b,reason=risk.reserve_entry("ETH-USD",100,300,max_per_hour=3,fraction=.25,now=now+1)
            self.assertEqual(reason,"reserved"); self.assertAlmostEqual(b['budget'],75)
            c,reason=risk.reserve_entry("SOL-USD",100,225,max_per_hour=3,fraction=.25,now=now+2)
            self.assertEqual(reason,"reserved"); self.assertAlmostEqual(c['budget'],56.25)
            d,reason=risk.reserve_entry("XRP-USD",100,168.75,max_per_hour=3,fraction=.25,now=now+3)
            self.assertIsNone(d); self.assertIn("hourly entry limit",reason)

if __name__ == '__main__': unittest.main()
