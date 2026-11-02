from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from huey.contrib.djhuey import HUEY as huey

from bookmarks.models import Bookmark
from bookmarks.services import tasks
from bookmarks.tests.helpers import BookmarkFactoryMixin


class LinkHealthTasksTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        huey.immediate = True
        huey.results = True
        huey.store_none = True

        self.mock_check_bookmarks_patcher = mock.patch(
            "bookmarks.services.link_health.check_bookmarks"
        )
        self.mock_check_bookmarks = self.mock_check_bookmarks_patcher.start()

        self.user = self.get_or_create_test_user()
        self.enable_link_health_checks(self.user)

    def tearDown(self):
        self.mock_check_bookmarks_patcher.stop()
        huey.storage.flush_results()
        huey.immediate = False

    def enable_link_health_checks(self, user, interval: int = 24):
        user.profile.enable_link_health_checks = True
        user.profile.link_health_check_interval = interval
        user.profile.save()

    def setup_checked_bookmark(self, hours_ago: float, **kwargs) -> Bookmark:
        bookmark = self.setup_bookmark(**kwargs)
        bookmark.link_last_checked = timezone.now() - timedelta(hours=hours_ago)
        bookmark.save()
        return bookmark

    def checked_ids(self) -> list[int]:
        ids = []
        for call in self.mock_check_bookmarks.call_args_list:
            ids.extend(call.args[0])
        return ids

    def run_scheduler(self):
        tasks._schedule_link_health_checks.call_local()

    def test_is_link_health_check_active(self):
        self.assertTrue(tasks.is_link_health_check_active(self.user))

        self.user.profile.enable_link_health_checks = False
        self.user.profile.save()
        self.assertFalse(tasks.is_link_health_check_active(self.user))

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_is_link_health_check_active_when_background_tasks_disabled(self):
        self.assertFalse(tasks.is_link_health_check_active(self.user))

    def test_check_link_health_checks_bookmarks(self):
        bookmark1 = self.setup_bookmark()
        bookmark2 = self.setup_bookmark()

        tasks.check_link_health([bookmark1.id, bookmark2.id], self.user)

        self.mock_check_bookmarks.assert_called_once_with([bookmark1.id, bookmark2.id])

    def test_check_link_health_runs_when_periodic_checks_disabled(self):
        self.user.profile.enable_link_health_checks = False
        self.user.profile.save()
        bookmark = self.setup_bookmark()

        tasks.check_link_health([bookmark.id], self.user)

        self.mock_check_bookmarks.assert_called_once_with([bookmark.id])

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_check_link_health_does_nothing_when_background_tasks_disabled(self):
        bookmark = self.setup_bookmark()

        tasks.check_link_health([bookmark.id], self.user)

        self.mock_check_bookmarks.assert_not_called()

    def test_check_link_health_enqueues_batches(self):
        bookmarks = [self.setup_bookmark() for _ in range(5)]
        ids = [bookmark.id for bookmark in bookmarks]

        with mock.patch.object(tasks, "LINK_HEALTH_CHECK_BATCH_SIZE", 2):
            tasks.check_link_health(ids, self.user)

        self.assertEqual(
            [call.args[0] for call in self.mock_check_bookmarks.call_args_list],
            [ids[0:2], ids[2:4], ids[4:5]],
        )

    def test_check_link_health_task_skips_bookmarks_checked_since_scheduling(self):
        scheduled_at = timezone.now() - timedelta(hours=1)
        unchecked = self.setup_bookmark()
        checked_before = self.setup_checked_bookmark(hours_ago=2)
        checked_after = self.setup_checked_bookmark(hours_ago=0.5)

        tasks._check_link_health_task(
            [unchecked.id, checked_before.id, checked_after.id],
            not_checked_since=scheduled_at,
        )

        self.assertCountEqual(self.checked_ids(), [unchecked.id, checked_before.id])

    def test_check_link_health_task_does_nothing_if_all_bookmarks_checked(self):
        bookmark = self.setup_checked_bookmark(hours_ago=0.5)

        tasks._check_link_health_task(
            [bookmark.id], not_checked_since=timezone.now() - timedelta(hours=1)
        )

        self.mock_check_bookmarks.assert_not_called()

    def test_schedule_checks_bookmarks_never_checked(self):
        bookmark1 = self.setup_bookmark()
        bookmark2 = self.setup_bookmark()

        self.run_scheduler()

        self.assertCountEqual(self.checked_ids(), [bookmark1.id, bookmark2.id])

    def test_schedule_checks_bookmarks_due_for_interval(self):
        due = self.setup_checked_bookmark(hours_ago=25)
        self.setup_checked_bookmark(hours_ago=23)

        self.run_scheduler()

        self.assertCountEqual(self.checked_ids(), [due.id])

    def test_schedule_respects_user_interval(self):
        self.enable_link_health_checks(self.user, interval=6)
        due = self.setup_checked_bookmark(hours_ago=7)
        self.setup_checked_bookmark(hours_ago=5)

        other_user = self.setup_user()
        self.enable_link_health_checks(other_user, interval=168)
        other_due = self.setup_checked_bookmark(hours_ago=169, user=other_user)
        self.setup_checked_bookmark(hours_ago=100, user=other_user)

        self.run_scheduler()

        self.assertCountEqual(self.checked_ids(), [due.id, other_due.id])

    def test_schedule_includes_archived_bookmarks(self):
        bookmark = self.setup_bookmark()
        archived = self.setup_bookmark(is_archived=True)

        self.run_scheduler()

        self.assertCountEqual(self.checked_ids(), [bookmark.id, archived.id])

    def test_schedule_skips_users_with_checks_disabled(self):
        bookmark = self.setup_bookmark()
        other_user = self.setup_user()
        self.setup_bookmark(user=other_user)

        self.run_scheduler()

        self.assertCountEqual(self.checked_ids(), [bookmark.id])

    def test_schedule_does_nothing_without_due_bookmarks(self):
        self.setup_checked_bookmark(hours_ago=1)

        self.run_scheduler()

        self.mock_check_bookmarks.assert_not_called()

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_schedule_does_nothing_when_background_tasks_disabled(self):
        self.setup_bookmark()

        self.run_scheduler()

        self.mock_check_bookmarks.assert_not_called()

    def test_schedule_enqueues_batches(self):
        bookmarks = [self.setup_bookmark() for _ in range(3)]

        with mock.patch.object(tasks, "LINK_HEALTH_CHECK_BATCH_SIZE", 2):
            self.run_scheduler()

        self.assertEqual(self.mock_check_bookmarks.call_count, 2)
        self.assertCountEqual(
            self.checked_ids(), [bookmark.id for bookmark in bookmarks]
        )

    def test_schedule_does_not_recheck_bookmarks_checked_before_task_runs(self):
        bookmark = self.setup_bookmark()

        # Capture the enqueued task instead of running it
        with mock.patch.object(tasks, "_check_link_health_task") as mock_task:
            self.run_scheduler()
        args, kwargs = mock_task.call_args
        self.assertEqual(args, ([bookmark.id],))

        # Another task checks the bookmark before the scheduled task runs
        Bookmark.objects.filter(id=bookmark.id).update(link_last_checked=timezone.now())
        tasks._check_link_health_task(*args, **kwargs)

        self.mock_check_bookmarks.assert_not_called()
