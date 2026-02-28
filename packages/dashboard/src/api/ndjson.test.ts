import { describe, it, expect } from 'vitest'
import { streamNDJSON, collectNDJSON } from './ndjson'

function makeResponse(body: string | ReadableStream<Uint8Array>, contentType = 'application/x-ndjson'): Response {
  const stream =
    typeof body === 'string'
      ? new ReadableStream({
          start(controller) {
            controller.enqueue(new TextEncoder().encode(body))
            controller.close()
          },
        })
      : body
  return new Response(stream, {
    headers: { 'Content-Type': contentType },
  })
}

function makeChunkedResponse(chunks: string[]): Response {
  const encoder = new TextEncoder()
  const stream = new ReadableStream({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(encoder.encode(chunk))
      }
      controller.close()
    },
  })
  return new Response(stream, {
    headers: { 'Content-Type': 'application/x-ndjson' },
  })
}

describe('streamNDJSON', () => {
  it('parses single-line NDJSON', async () => {
    const response = makeResponse('{"id":1,"name":"test"}\n')
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([{ id: 1, name: 'test' }])
  })

  it('parses multiple lines', async () => {
    const body = '{"id":1}\n{"id":2}\n{"id":3}\n'
    const response = makeResponse(body)
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([{ id: 1 }, { id: 2 }, { id: 3 }])
  })

  it('handles line without trailing newline', async () => {
    const body = '{"id":1}\n{"id":2}'
    const response = makeResponse(body)
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([{ id: 1 }, { id: 2 }])
  })

  it('skips blank lines', async () => {
    const body = '{"id":1}\n\n\n{"id":2}\n'
    const response = makeResponse(body)
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([{ id: 1 }, { id: 2 }])
  })

  it('handles chunked delivery splitting a JSON line', async () => {
    // Simulate a JSON object split across two chunks
    const response = makeChunkedResponse(['{"id":1}\n{"na', 'me":"split"}\n'])
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([{ id: 1 }, { name: 'split' }])
  })

  it('yields nothing when response body is null', async () => {
    // Create a response with no body
    const response = new Response(null)
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([])
  })

  it('yields nothing for empty stream', async () => {
    const response = makeResponse('')
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([])
  })

  it('handles whitespace-only lines', async () => {
    const body = '{"id":1}\n   \n{"id":2}\n'
    const response = makeResponse(body)
    const items: unknown[] = []
    for await (const item of streamNDJSON(response)) {
      items.push(item)
    }
    expect(items).toEqual([{ id: 1 }, { id: 2 }])
  })

  it('throws on invalid JSON', async () => {
    const body = '{"id":1}\nnot-json\n'
    const response = makeResponse(body)
    const items: unknown[] = []
    await expect(async () => {
      for await (const item of streamNDJSON(response)) {
        items.push(item)
      }
    }).rejects.toThrow()
    // First item should have been yielded before the error
    expect(items).toEqual([{ id: 1 }])
  })
})

describe('collectNDJSON', () => {
  it('collects all items into an array', async () => {
    const body = '{"id":1}\n{"id":2}\n{"id":3}\n'
    const response = makeResponse(body)
    const result = await collectNDJSON(response)
    expect(result).toEqual([{ id: 1 }, { id: 2 }, { id: 3 }])
  })

  it('returns empty array for empty stream', async () => {
    const response = makeResponse('')
    const result = await collectNDJSON(response)
    expect(result).toEqual([])
  })

  it('returns empty array when body is null', async () => {
    const response = new Response(null)
    const result = await collectNDJSON(response)
    expect(result).toEqual([])
  })
})
