/** Build YAML frontmatter + markdown content for a saved article. */
export function buildNoteContent(article: {
  url: string;
  hostname: string;
  byline?: string;
  siteName?: string;
  publishedTime?: string;
  markdown: string;
}): string {
  const lines: string[] = [
    '---',
    `source_url: ${article.url}`,
    `hostname: ${article.hostname}`,
  ];
  if (article.byline) lines.push(`author: ${article.byline}`);
  if (article.siteName) lines.push(`site_name: ${article.siteName}`);
  if (article.publishedTime) lines.push(`publish_date: ${article.publishedTime}`);
  lines.push('---', '');
  return lines.join('\n') + article.markdown;
}
