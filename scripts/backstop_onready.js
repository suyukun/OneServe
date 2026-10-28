/**
 * BackstopJS onReadyScript: inject admin auth token into localStorage
 * before screenshot capture to bypass login wall on admin pages.
 */
module.exports = async (page, scenario, vp) => {
  // Inject auth token
  await page.evaluate(() => {
    localStorage.setItem('oneserve-oidc-token', 'admin');
  });
  // Wait for page to settle (use setTimeout since waitForTimeout is deprecated)
  await new Promise(r => setTimeout(r, 1500));
};
