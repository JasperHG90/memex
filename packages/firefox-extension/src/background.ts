/**
 * Background script for Save to Memex.
 * Currently minimal — handles extension install and future context-menu support.
 */

browser.runtime.onInstalled.addListener(() => {
  console.log('Save to Memex extension installed.');
});
