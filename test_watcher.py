import unittest
from datetime import date, datetime
from watcher import BERLIN, qualifies, price_cents, minutes, weekends, key

class FilterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 25, 10, tzinfo=BERLIN)
        self.sat = date(2026, 11, 28)
    def q(self, start=600, duration=60, cents=1600, **kwargs):
        return qualifies(self.sat, start, duration, cents, now=self.now, **kwargs)
    def test_strict_price(self):
        self.assertTrue(self.q(cents=1999))
        self.assertFalse(self.q(cents=2000))
        self.assertTrue(self.q(duration=90, cents=2999))
        self.assertFalse(self.q(duration=90, cents=3000))
    def test_start_not_end(self):
        self.assertTrue(self.q(start=1260, duration=90, cents=2400))
        self.assertFalse(self.q(start=1261))
        self.assertFalse(self.q(start=599))
    def test_double_and_duration(self):
        self.assertFalse(self.q(double=False))
        self.assertFalse(self.q(duration=120))
    def test_weekday_and_past(self):
        self.assertFalse(qualifies(date(2026,11,26),600,60,1600,now=self.now))
        self.assertFalse(qualifies(date(2026,11,22),600,60,1600,now=self.now))
    def test_ui_price_and_time(self):
        self.assertEqual(price_cents('1 hr, 30 min\n€24.00'), 2400)
        self.assertEqual(price_cents('€19,99'),1999)
        self.assertEqual(minutes('9:00 PM'),1260)
        self.assertEqual(minutes('12:00 PM'),720)
        self.assertEqual(minutes('12:00 AM'),0)
        with self.assertRaises(RuntimeError): price_cents('Preis unbekannt')
    def test_horizon(self):
        self.assertEqual(weekends(date(2026,11,25),4),[date(2026,11,28),date(2026,11,29)])
    def test_dedup_includes_price(self):
        self.assertNotEqual(key({'cents':1600}), key({'cents':1800}))

if __name__ == '__main__': unittest.main()
