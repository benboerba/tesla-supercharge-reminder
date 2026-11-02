import unittest
from unittest.mock import patch
from tesla_watch import classify, validate_callback, sites


class MonitorTests(unittest.TestCase):
    def test_sites_requests_occupancy_details(self):
        with patch('tesla_watch.api', return_value={'superchargers': []}) as call:
            sites('test-vin')
            self.assertIn('detail=true', call.call_args[0][0])

    def data(self, available=3):
        return {'congestion_sync_time_utc_secs': 1000, 'superchargers': [
            {'id': 7, 'available_stalls': available, 'total_stalls': 8, 'site_closed': False}]}

    def test_capacity_transitions(self):
        for available, expected in [(8, 'available'), (2, 'low'), (1, 'low'), (0, 'full')]:
            self.assertEqual(classify(self.data(available), '7', 1001)[0], expected)

    def test_stale_full_is_unknown(self):
        self.assertEqual(classify(self.data(0), '7', 1400)[0], 'unknown')
        status,message=classify(self.data(3), '7', 1540)
        self.assertEqual(status,'unknown')
        self.assertIn('空闲 3 / 总桩 8',message)
        self.assertIn('08:16:40（北京时间）',message)
        self.assertIn('暂未更新',message)

    def test_missing_freshness_is_unknown(self):
        data = self.data()
        data.pop('congestion_sync_time_utc_secs')
        self.assertEqual(classify(data, '7', 1001)[0], 'unknown')

    def test_invalid_values_are_not_free_spaces(self):
        for value in [-1, None, True, '3', 9]:
            self.assertEqual(classify(self.data(value), '7', 1001)[0], 'unknown')

    def test_missing_or_closed_station(self):
        self.assertEqual(classify(self.data(), '9', 1001)[0], 'unknown')
        data = self.data()
        data['superchargers'][0]['site_closed'] = True
        self.assertEqual(classify(data, '7', 1001)[0], 'unknown')

    def test_oauth_rejects_wrong_state_origin_and_reuse(self):
        config = {'redirect_uri': 'https://example.com/auth/callback'}
        pending = {'state': 'abc', 'created': 1000}
        self.assertEqual(validate_callback('https://example.com/auth/callback?code=x&state=abc', config, pending, 1001), 'x')
        for url in ['https://example.com/auth/callback?code=x&state=wrong',
                    'https://evil.example/auth/callback?code=x&state=abc']:
            with self.assertRaises(RuntimeError): validate_callback(url, config, pending, 1001)
        for changed, now in [({'state': 'abc', 'created': 1000, 'used': True}, 1001), (pending, 1700)]:
            with self.assertRaises(RuntimeError):
                validate_callback('https://example.com/auth/callback?code=x&state=abc', config, changed, now)


if __name__ == '__main__':
    unittest.main()
