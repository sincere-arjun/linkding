from unittest.mock import patch

from django.urls import reverse
from django.utils import formats, timezone
from playwright.sync_api import expect

from bookmarks.models import Bookmark
from bookmarks.services import tasks
from bookmarks.tests_e2e.helpers import LinkdingE2ETestCase


class LinkHealthE2ETestCase(LinkdingE2ETestCase):
    def setup_broken_bookmark(self, title: str, **kwargs):
        bookmark = self.setup_bookmark(title=title, **kwargs)
        bookmark.link_status_code = 404
        bookmark.link_failure_count = 3
        bookmark.link_last_checked = timezone.now()
        bookmark.link_broken = True
        bookmark.save()
        return bookmark

    def test_link_health_settings(self):
        self.open(reverse("linkding:settings.general"))

        section = self.page.get_by_role("group", name="Link health")
        expect(section).to_be_visible()

        enable_checks = section.get_by_label("Enable periodic link health checks")
        interval = section.get_by_label("Check interval")
        expect(enable_checks).not_to_be_checked()
        expect(interval).to_have_value("24")

        section.get_by_text("Enable periodic link health checks").click()
        interval.select_option(label="Weekly")
        self.page.get_by_role("button", name="Save", exact=True).click()

        expect(self.page.get_by_text("Profile updated")).to_be_visible()
        profile = self.get_or_create_test_user().profile
        profile.refresh_from_db()
        self.assertTrue(profile.enable_link_health_checks)
        self.assertEqual(profile.link_health_check_interval, 168)

        # Saved values are shown after reload
        section = self.page.get_by_role("group", name="Link health")
        expect(
            section.get_by_label("Enable periodic link health checks")
        ).to_be_checked()
        expect(section.get_by_label("Check interval")).to_have_value("168")

    def test_broken_badge(self):
        self.setup_broken_bookmark("Broken bookmark")
        self.setup_bookmark(title="Healthy bookmark")

        self.open(reverse("linkding:bookmarks.index"))

        broken_badge = self.locate_bookmark("Broken bookmark").locator(
            ".link-broken-badge"
        )
        expect(broken_badge).to_be_visible()
        expect(broken_badge).to_have_text("Broken")
        reason = "Status code 404, last checked: " + self._last_checked_text()
        expect(broken_badge).to_have_attribute("title", reason)
        # The reason is also available to screen readers, not only on hover
        expect(broken_badge).to_have_accessible_description(reason)
        expect(
            self.locate_bookmark("Healthy bookmark").locator(".link-broken-badge")
        ).to_have_count(0)

    def test_no_broken_badge_for_other_users_shared_bookmarks(self):
        other_user = self.setup_user(enable_sharing=True)
        self.setup_broken_bookmark(
            "Other user's broken bookmark", shared=True, user=other_user
        )
        profile = self.get_or_create_test_user().profile
        profile.enable_sharing = True
        profile.save()

        self.open(reverse("linkding:bookmarks.shared") + "?broken=yes")

        bookmark = self.locate_bookmark("Other user's broken bookmark")
        expect(bookmark).to_be_visible()
        expect(bookmark.locator(".link-broken-badge")).to_have_count(0)
        expect(self.page.locator(".link-broken-badge")).to_have_count(0)
        expect(self.page.get_by_text("Status code 404")).to_have_count(0)

    def test_broken_filter(self):
        self.setup_broken_bookmark("Broken bookmark")
        self.setup_bookmark(title="Healthy bookmark")

        self.open(reverse("linkding:bookmarks.index"))
        expect(self.locate_bookmark("Healthy bookmark")).to_be_visible()

        self.page.get_by_role("button", name="Search preferences").click()
        filter_group = self.page.get_by_role("radiogroup", name="Link health filter")
        filter_group.get_by_text("Broken").click()
        self.page.get_by_role("button", name="Apply").click()

        expect(self.page).to_have_url(
            self.live_server_url + reverse("linkding:bookmarks.index") + "?broken=yes"
        )
        expect(self.locate_bookmark("Broken bookmark")).to_be_visible()
        expect(self.locate_bookmark("Healthy bookmark")).to_have_count(0)

    def test_check_links_bulk_action(self):
        bookmark1 = self.setup_bookmark(title="Bookmark 1")
        self.setup_bookmark(title="Bookmark 2")
        bookmark3 = self.setup_broken_bookmark("Bookmark 3")

        # Mock the task so that no actual HTTP requests are made
        with patch.object(tasks, "_check_link_health_task") as mock_task:
            self.open(reverse("linkding:bookmarks.index"))

            bookmark_list = self.locate_bookmark_list().element_handle()
            self.locate_bulk_edit_toggle().click()
            self.locate_bookmark("Bookmark 1").locator(
                "label.bulk-edit-checkbox"
            ).click()
            self.locate_bookmark("Bookmark 3").locator(
                "label.bulk-edit-checkbox"
            ).click()

            self.select_bulk_action("Check links")
            self.locate_bulk_edit_bar().get_by_text("Execute").click()
            self.locate_confirm_dialog().get_by_text("Confirm").click()
            # Wait until bookmark list is updated (old reference becomes invisible)
            bookmark_list.wait_for_element_state("hidden", timeout=2000)

            mock_task.assert_called_once()
            self.assertCountEqual(
                mock_task.call_args.args[0], [bookmark1.id, bookmark3.id]
            )

    def _last_checked_text(self):
        bookmark = Bookmark.objects.get(title="Broken bookmark")
        return formats.date_format(
            timezone.localtime(bookmark.link_last_checked), "SHORT_DATETIME_FORMAT"
        )
