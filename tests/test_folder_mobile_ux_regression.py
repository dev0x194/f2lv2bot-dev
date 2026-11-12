import os
import re
import jinja2
import pytest
from unittest.mock import patch
from bson import ObjectId

from FileStream.server.render_template import render_folder
from tests.test_folder_mode import MockDatabase, MockBot


@pytest.fixture
def mock_db():
    return MockDatabase()


@pytest.fixture
def mock_bot():
    return MockBot()


@pytest.fixture
def repo_static_paths():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(current_dir)
    folder_html = os.path.join(repo_root, "FileStream", "web", "templates", "folder.html")
    if not os.path.exists(folder_html):
        folder_html = os.path.join(repo_root, "FileStream", "template", "folder.html")
    main_css = os.path.join(repo_root, "FileStream", "web", "static", "main.css")
    if not os.path.exists(main_css):
        main_css = os.path.join(repo_root, "FileStream", "static", "main.css")
    app_js = os.path.join(repo_root, "FileStream", "web", "static", "app.js")
    if not os.path.exists(app_js):
        app_js = os.path.join(repo_root, "FileStream", "static", "app.js")
    return {
        "folder_html": folder_html,
        "main_css": main_css,
        "app_js": app_js,
    }


def test_folder_template_structure_and_action_drawer(repo_static_paths):
    """1. Folder template contains bottom sheet action drawer and backdrop outside cards grid."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    assert 'id="fileCardsGrid"' in content
    assert 'id="actionBackdrop"' in content
    assert 'class="fs-action-backdrop hidden"' in content
    assert 'id="actionPanel"' in content
    assert 'class="fs-selected-panel hidden"' in content
    assert 'id="actionCloseBtn"' in content
    assert 'class="fs-sheet-handle"' in content
    assert 'aria-live="polite"' in content


def test_folder_cards_consistent_structure_no_inline_detail_panel(repo_static_paths):
    """2. File cards have uniform compact layout and do not inline-expand or duplicate names."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Verify card template in JS
    assert 'class="fs-file-card-item' in content
    assert 'class="fs-file-card-top"' in content
    assert 'class="fs-file-card-icon"' in content
    assert 'class="fs-file-card-details"' in content
    assert 'class="fs-file-card-title"' in content
    assert 'class="fs-file-card-meta"' in content
    assert 'class="fs-file-card-size"' in content
    assert 'class="fs-file-card-cat"' in content
    assert 'class="fs-file-card-arrow"' in content

    # CSS check: file card title truncates with ellipsis
    assert "text-overflow: ellipsis" in content
    assert "white-space: nowrap" in content

    # File cards must be role="button" with tabindex="0" and aria-pressed
    assert 'role="button"' in content
    assert 'tabindex="0"' in content
    assert 'aria-pressed="' in content


def test_selected_file_uses_mobile_bottom_sheet(repo_static_paths):
    """3. Selected file state uses dedicated mobile bottom sheet drawer instead of expanding cards."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Verify CSS contains fixed bottom drawer layout on mobile
    assert "position: fixed;" in content
    assert "bottom: 0;" in content
    assert any(f"border-radius: {r} {r} 0 0;" in content for r in ("var(--fs-radius-xl)", "var(--fs-radius-lg)"))
    assert "fsSlideUp" in content
    assert "grid-template-columns: 1fr 1fr !important;" in content

    # Verify deselect and escape key bindings
    assert "deselectFile" in content
    assert "actionCloseBtn.addEventListener('click', deselectFile)" in content
    assert "actionBackdrop.addEventListener('click', deselectFile)" in content


def test_mobile_header_overflow_protection(repo_static_paths):
    """4. Top navigation header has flex-shrinking and responsive compact Join label."""
    with open(repo_static_paths["main_css"], "r", encoding="utf-8") as f:
        css = f.read()

    assert ".fs-header-actions" in css
    assert ".fs-header-join-btn" in css
    assert ".fs-join-label-short" in css
    assert "display: none;" in css
    assert "@media (max-width: 480px)" in css
    assert ".fs-join-label-full" in css

    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        html = f.read()

    assert "fs-header-actions" in html
    assert "fs-header-join-btn" in html
    assert "fs-join-label-full" in html
    assert "fs-join-label-short" in html


def test_search_and_sort_controls_mobile_spacing(repo_static_paths):
    """5. Search and sort row remains responsive, single-row at 360px-600px, and stacks cleanly at <=360px."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Inputs and selects have accessible touch height and responsive width
    assert ".fs-search-input" in content
    assert any(f"min-height: {h}px;" in content for h in (38, 44))
    assert ".fs-sort-select" in content
    assert any(f"flex: 0 0 {w}px;" in content for w in (135, 160))
    # Stacking at <= 360px
    assert "@media (max-width: 360px)" in content
    assert "flex-direction: column" in content


def test_category_chips_wrapping_and_attributes(repo_static_paths):
    """6. Category chips wrap cleanly with no horizontal scrollbar."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Must have flex-wrap: wrap
    assert "flex-wrap: wrap;" in content
    # Ensure overflow-x: auto was removed from chips to avoid scrollbars
    chip_css_match = re.search(r"\.fs-filter-chips\s*\{([^}]+)\}", content)
    assert chip_css_match is not None
    assert "overflow-x: auto" not in chip_css_match.group(1)

    # Radiogroup & roles
    assert 'role="radiogroup"' in content
    assert 'role="radio"' in content
    assert 'aria-checked=' in content


def test_touch_targets_and_accessibility(repo_static_paths):
    """7. Touch targets meet minimum accessible dimensions and semantic button attributes."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    assert 'aria-label="Close file actions"' in content
    assert 'aria-label="Search files in folder"' in content
    assert 'aria-label="Sort files"' in content
    assert 'role="group"' in content
    assert 'aria-label="File Actions"' in content


def test_ad_card_graceful_collapse(repo_static_paths):
    """8. Ad card collapses gracefully when slots fail without leaving empty borders."""
    with open(repo_static_paths["main_css"], "r", encoding="utf-8") as f:
        css = f.read()

    assert ".fs-ad-card.ad-card-collapsed" in css
    assert "display: none !important;" in css

    with open(repo_static_paths["app_js"], "r", encoding="utf-8") as f:
        js = f.read()

    assert "ad-card-collapsed" in js


@pytest.mark.asyncio
async def test_render_folder_full_flow_with_multiple_files(mock_db, mock_bot):
    """9. Render folder page with multiple files verifies unified cards and safe tojson."""
    with patch("FileStream.server.render_template.db", mock_db), \
         patch("FileStream.server.render_template.get_bot_username", return_value="file2linkv2bot"):
        fids = []
        for i in range(3):
            fid = ObjectId()
            file_data = {
                "_id": fid,
                "file_name": f"Season.01.Episode.{i+1:02d}.mkv",
                "file_size": 250000000 * (i + 1),
                "mime_type": "video/x-matroska",
                "uploader": "TestUploader",
            }
            await mock_db.file.insert_one(file_data)
            fids.append(str(fid))

        folder_id = ObjectId()
        folder_doc = {
            "_id": folder_id,
            "title": "The Last Ship Season 1",
            "files": fids,
        }
        await mock_db.folders.insert_one(folder_doc)
        public_link = {"public_id": "gfolder_tls"}

        html = await render_folder(
            str(folder_id),
            title="The Last Ship Season 1",
            folder_doc=folder_doc,
            public_link=public_link,
            session_id="session_folder_test",
        )

        assert "<!DOCTYPE html>" in html
        assert "The Last Ship Season 1" in html
        assert "Season.01.Episode.01.mkv" in html
        assert "Season.01.Episode.02.mkv" in html
        assert "Season.01.Episode.03.mkv" in html
        assert "actionBackdrop" in html
        assert "actionPanel" in html
        assert "actionCloseBtn" in html
        assert "fs-header-join-btn" in html
        assert "fs-join-label-full" in html
        assert "fs-join-label-short" in html
        assert "folderTotalSizeBadge" in html


def test_container_system_gutters_and_responsive_shell(repo_static_paths):
    """10. Container system implements tiered gutters and responsive shell."""
    with open(repo_static_paths["main_css"], "r", encoding="utf-8") as f:
        css = f.read()

    assert "calc(100% - 24px)" in css or "calc(100% - 48px)" in css
    assert ".fs-container" in css


def test_ad_iframe_overflow_protection(repo_static_paths):
    """11. Ad cards and iframes are strictly constrained to 100% max-width with box-sizing."""
    with open(repo_static_paths["main_css"], "r", encoding="utf-8") as f:
        css = f.read()

    assert "img, svg, video, iframe" in css
    assert ".fs-ad-card iframe" in css
    assert "max-width: 100% !important;" in css


def test_desktop_docked_action_panel_and_mobile_sheet(repo_static_paths):
    """12. Action panel provides docked horizontal deck on desktop and bottom sheet on mobile."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    assert "display: flex;" in content
    assert "flex-wrap: wrap;" in content
    assert "display: grid !important;" in content
    assert "grid-template-columns: 1fr 1fr !important;" in content
    assert "aria-expanded=" in content


def test_folder_header_desktop_and_mobile_rhythm(repo_static_paths):
    """13. Folder header card has full-width share action on mobile and neat horizontal layout on desktop."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    assert ".fs-folder-header-card" in content
    assert "@media (max-width: 600px)" in content
    assert ".fs-folder-share-btn" in content
    assert "width: 100%;" in content


def test_mobile_file_card_width_containment_and_box_sizing(repo_static_paths):
    """14. File card item and cards grid strictly enforce 100% max-width, min-width: 0, box-sizing, and overflow containment."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Extract .fs-cards-grid CSS rule
    grid_match = re.search(r"\.fs-cards-grid\s*\{([^}]+)\}", content)
    assert grid_match is not None, ".fs-cards-grid rule must exist"
    grid_css = grid_match.group(1)
    assert "grid-template-columns: minmax(0, 1fr);" in grid_css
    assert "width: 100%;" in grid_css
    assert "max-width: 100%;" in grid_css
    assert "min-width: 0;" in grid_css
    assert "box-sizing: border-box;" in grid_css

    # Extract .fs-file-card-item CSS rule
    card_match = re.search(r"\.fs-file-card-item\s*\{([^}]+)\}", content)
    assert card_match is not None, ".fs-file-card-item rule must exist"
    card_css = card_match.group(1)
    assert "width: 100%;" in card_css
    assert "max-width: 100%;" in card_css
    assert "min-width: 0;" in card_css
    assert "box-sizing: border-box;" in card_css
    assert "overflow: hidden;" in card_css


def test_mobile_file_card_grid_layout_and_middle_column_constraint(repo_static_paths):
    """15. Card top layout uses 3-column grid with minmax(0, 1fr) middle track to prevent card blowout."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Extract .fs-file-card-top CSS rule
    top_match = re.search(r"\.fs-file-card-top\s*\{([^}]+)\}", content)
    assert top_match is not None, ".fs-file-card-top rule must exist"
    top_css = top_match.group(1)
    assert "display: grid;" in top_css
    assert "grid-template-columns: auto minmax(0, 1fr) auto;" in top_css
    assert "min-width: 0;" in top_css
    assert "box-sizing: border-box;" in top_css

    # Extract .fs-file-card-details CSS rule
    details_match = re.search(r"\.fs-file-card-details\s*\{([^}]+)\}", content)
    assert details_match is not None, ".fs-file-card-details rule must exist"
    details_css = details_match.group(1)
    assert "min-width: 0;" in details_css
    assert "max-width: 100%;" in details_css
    assert "overflow: hidden;" in details_css


def test_long_filename_wrapping_and_ellipsis_containment(repo_static_paths):
    """16. Long filenames (dot-separated or space-separated) cannot blow out the card width."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Extract .fs-file-card-title CSS rule
    title_match = re.search(r"\.fs-file-card-title\s*\{([^}]+)\}", content)
    assert title_match is not None, ".fs-file-card-title rule must exist"
    title_css = title_match.group(1)
    assert "width: 100%;" in title_css
    assert "max-width: 100%;" in title_css
    assert "min-width: 0;" in title_css
    assert "overflow: hidden;" in title_css
    assert "text-overflow: ellipsis;" in title_css
    assert "white-space: nowrap;" in title_css
    assert "word-break: break-all;" in title_css
    assert "overflow-wrap: anywhere;" in title_css


def test_file_card_chevron_non_shrink_and_metadata_containment(repo_static_paths):
    """17. Trailing chevron is non-shrinking, and metadata row cleanly constrains category."""
    with open(repo_static_paths["folder_html"], "r", encoding="utf-8") as f:
        content = f.read()

    # Extract .fs-file-card-arrow CSS rule
    arrow_match = re.search(r"\.fs-file-card-arrow\s*\{([^}]+)\}", content)
    assert arrow_match is not None, ".fs-file-card-arrow rule must exist"
    arrow_css = arrow_match.group(1)
    assert "flex: 0 0 auto;" in arrow_css

    # Extract .fs-file-card-icon CSS rule
    icon_match = re.search(r"\.fs-file-card-icon\s*\{([^}]+)\}", content)
    assert icon_match is not None, ".fs-file-card-icon rule must exist"
    icon_css = icon_match.group(1)
    assert "flex: 0 0 auto;" in icon_css

    # Extract .fs-file-card-meta CSS rule
    meta_match = re.search(r"\.fs-file-card-meta\s*\{([^}]+)\}", content)
    assert meta_match is not None, ".fs-file-card-meta rule must exist"
    meta_css = meta_match.group(1)
    assert "min-width: 0;" in meta_css
    assert "max-width: 100%;" in meta_css
    assert "overflow: hidden;" in meta_css

    # Extract .fs-file-card-cat CSS rule
    cat_match = re.search(r"\.fs-file-card-cat\s*\{([^}]+)\}", content)
    assert cat_match is not None, ".fs-file-card-cat rule must exist"
    cat_css = cat_match.group(1)
    assert "overflow: hidden;" in cat_css
    assert "text-overflow: ellipsis;" in cat_css
    assert "min-width: 0;" in cat_css


def test_shell_card_and_root_viewport_overflow_protection(repo_static_paths):
    """18. html, body, shell, and card enforce overflow-x: hidden and max-width: 100% across viewports."""
    with open(repo_static_paths["main_css"], "r", encoding="utf-8") as f:
        content = f.read()

    # html and body overflow-x: hidden
    assert "overflow-x: hidden;" in content
    assert "max-width: 100%;" in content

    # .fs-shell
    shell_match = re.search(r"\.fs-shell\s*\{([^}]+)\}", content)
    assert shell_match is not None, ".fs-shell rule must exist"
    shell_css = shell_match.group(1)
    assert "overflow-x: hidden;" in shell_css
    assert "width: 100%;" in shell_css
    assert "max-width: 100%;" in shell_css
    assert "box-sizing: border-box;" in shell_css

    # .fs-card base
    card_match = re.search(r"\.fs-card\s*\{([^}]+)\}", content)
    assert card_match is not None, ".fs-card rule must exist"
    card_css = card_match.group(1)
    assert "width: 100%;" in card_css
    assert "max-width: 100%;" in card_css
    assert "min-width: 0;" in card_css
    assert "box-sizing: border-box;" in card_css


@pytest.mark.asyncio
async def test_render_folder_with_extreme_long_filenames(mock_db, mock_bot):
    """19. Render folder page with extreme long filenames containing dots, spaces, and special chars."""
    with patch("FileStream.server.render_template.db", mock_db), \
         patch("FileStream.server.render_template.get_bot_username", return_value="file2linkv2bot"):
        long_names = [
            "The.Last.Ship.S05E09.Courage.720p.WEBRip.2CH.x265.HEVC-PSA.mkv",
            "The Last Ship S05E10 Commitment 720p WEBRip 2CH x265 HEVC PSA.mkv",
            "Extremely.Long.File.Name.With.Many.Consecutive.Dots.And.Numbers.2026.1080p.BluRay.DTS-HD.MA.5.1.x264-GRP.mkv",
        ]
        fids = []
        for i, fname in enumerate(long_names):
            fid = ObjectId()
            file_data = {
                "_id": fid,
                "file_name": fname,
                "file_size": 350000000 * (i + 1),
                "mime_type": "video/x-matroska",
                "uploader": "UploaderBot",
            }
            await mock_db.file.insert_one(file_data)
            fids.append(str(fid))

        folder_id = ObjectId()
        folder_doc = {
            "_id": folder_id,
            "title": "Extreme Long Name Test Folder",
            "files": fids,
        }
        await mock_db.folders.insert_one(folder_doc)
        public_link = {"public_id": "gfolder_long_names"}

        html = await render_folder(
            str(folder_id),
            title="Extreme Long Name Test Folder",
            folder_doc=folder_doc,
            public_link=public_link,
            session_id="session_long_names",
        )

        assert "<!DOCTYPE html>" in html
        for fname in long_names:
            assert fname in html
        # Confirm action drawer elements exist
        assert 'id="fileCardsGrid"' in html
        assert 'id="actionPanel"' in html
        assert 'id="actionBackdrop"' in html
