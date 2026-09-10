"""Browser tests for the virtualized spreadsheet component (base.html) used
by the Influencer CRM and Brand CRM tables.

These exist to catch the regression class the 2026-07 pagination rewrite was
built to avoid: rows losing sync when moved out of the live DOM, filters/sort
only seeing whatever page happens to be rendered, deleted rows reappearing.
A plain Flask test_client() can't catch any of this -- filtering, sorting,
and pagination all run client-side in JS, so these drive a real browser.
"""


def test_table_only_renders_one_page_of_rows(page, base_url):
    page.goto(base_url + "/crm/influencers")
    page.wait_for_selector("table[data-sheet] tbody.data tr[data-id]")
    live_rows = page.locator("table[data-sheet] tbody.data tr")
    assert live_rows.count() <= 50, "only the current page should be attached to the DOM"

    node_count = page.evaluate("document.querySelectorAll('*').length")
    # 71 seeded rows fully rendered (pre-fix behaviour) blew past 20k nodes
    # on real data; stay well under that regardless of dataset size.
    assert node_count < 3000, f"DOM node count should stay small, got {node_count}"

    assert "Page 1 of 2" in page.locator(".pager").inner_text()


def test_search_finds_a_row_on_a_different_page(page, base_url):
    page.goto(base_url + "/crm/influencers")
    # Force a deterministic full-dataset sort: alphabetical by name.
    # "Needle Zzyzx" sorts after all 70 "Creator NNN" rows, landing on page 2.
    page.click("thead th:has-text('Influencer')")
    page.wait_for_selector("text=Page 1 of 2")
    assert page.locator("table[data-sheet] tbody.data tr", has_text="Needle Zzyzx").count() == 0, \
        "sanity check: Needle should not be on page 1 before searching"

    page.fill("#inflSearch", "Needle Zzyzx")
    page.locator("table[data-sheet] tbody.data tr", has_text="Needle Zzyzx").first.wait_for()
    rows = page.locator("table[data-sheet] tbody.data tr")
    assert rows.count() == 1
    assert "Showing 1" in page.locator(".pager").inner_text()


def test_column_filter_narrows_the_full_dataset(page, base_url):
    page.goto(base_url + "/crm/brands")
    page.click("thead th:has-text('Brand')")   # deterministic ascending sort
    page.wait_for_selector("text=Page 1 of 2")
    assert page.locator("table[data-sheet] tbody.data tr", has_text="Brand 069").count() == 0, \
        "sanity check: Brand 069 should not be on page 1 before filtering"

    filter_box = page.locator("thead tr.filterrow td:nth-child(2) input.colfilter")
    filter_box.fill("Brand 069")
    page.locator("table[data-sheet] tbody.data tr", has_text="Brand 069").first.wait_for()
    rows = page.locator("table[data-sheet] tbody.data tr")
    assert rows.count() == 1


def test_select_filter_checks_the_full_dataset(page, base_url):
    page.goto(base_url + "/crm/influencers")
    page.click("thead th:has-text('Influencer')")
    page.wait_for_selector("text=Page 1 of 2")

    page.click(".mfbtn")  # Status multi-select dropdown
    page.click(".mfpanel label:has-text('New')")
    page.wait_for_timeout(150)

    rows = page.locator("table[data-sheet] tbody.data tr")
    # 35 "New" creators + the Needle row (also "New") = 36, all fit on one page
    assert rows.count() == 36
    assert page.locator("table[data-sheet] tbody.data tr", has_text="Needle Zzyzx").count() == 1


def test_deleted_row_does_not_reappear_after_paging(page, base_url):
    page.goto(base_url + "/crm/brands")
    page.click("thead th:has-text('Brand')")
    page.wait_for_selector("text=Page 1 of 2")

    first_row = page.locator("table[data-sheet] tbody.data tr").first
    brand_text = first_row.locator('td[data-f="brand"]').inner_text()

    page.once("dialog", lambda d: d.accept())
    first_row.locator("button.danger").click()
    page.wait_for_timeout(500)  # 250ms fade-out + re-render

    assert "of 70" in page.locator(".pager").inner_text()

    page.fill("#brandSearch", brand_text)
    page.wait_for_timeout(200)
    assert page.locator("table[data-sheet] tbody.data tr", has_text=brand_text).count() == 0, \
        "deleted row should not resurface via search"
