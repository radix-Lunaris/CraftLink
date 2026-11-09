"""Reject broader pushes than the single user-approved dev commit."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from check_push import validate_updates


class PushGuardTests(unittest.TestCase):
    def setUp(self):
        self.remote = 'https://github.com/radix-Lunaris/CraftLink.git'
        self.approval = {'status':'approved','commit':'1'*40,'previous':'2'*40}
        self.updates = [['refs/heads/dev','1'*40,'refs/heads/dev','2'*40]]

    def test_exact_authorized_dev_update(self):
        self.assertEqual(validate_updates(self.remote,self.updates,self.approval),('1'*40,'2'*40))

    def test_reject_other_refs_commits_remotes_and_consumed_approval(self):
        variants = [
            (self.remote, [['refs/heads/master','1'*40,'refs/heads/master','2'*40]], self.approval),
            (self.remote, self.updates*2, self.approval),
            (self.remote, [['refs/heads/dev','3'*40,'refs/heads/dev','2'*40]], self.approval),
            (self.remote, [['refs/heads/dev','1'*40,'refs/heads/dev','0'*40]], self.approval),
            ('https://github.com/unexpected/CraftLink.git',self.updates,self.approval),
            (self.remote,self.updates,dict(self.approval,status='consumed')),
        ]
        for args in variants:
            with self.subTest(args=args), self.assertRaises(ValueError):validate_updates(*args)


if __name__ == '__main__':unittest.main()
