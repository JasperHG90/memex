export async function* streamNDJSON<T>(response: Response): AsyncGenerator<T> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    for (const line of lines) {
      if (line.trim()) yield JSON.parse(line) as T;
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer) as T;
}

export async function collectNDJSON<T>(response: Response): Promise<T[]> {
  const items: T[] = [];
  for await (const item of streamNDJSON<T>(response)) {
    items.push(item);
  }
  return items;
}
