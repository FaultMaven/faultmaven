// Simple in-memory rate limiter (per Vercel instance)
// TODO: Use a distributed/global rate limiter (e.g., Redis) for production

const rateLimitMap = new Map<string, { count: number; lastRequest: number }>();
const WINDOW_MS = 60 * 1000; // 1 minute
const MAX_REQUESTS = 10; // 10 requests per minute per IP

export function rateLimit(ip: string): boolean {
  const now = Date.now();
  const entry = rateLimitMap.get(ip);
  if (!entry || now - entry.lastRequest > WINDOW_MS) {
    rateLimitMap.set(ip, { count: 1, lastRequest: now });
    return false; // not rate limited
  }
  if (entry.count >= MAX_REQUESTS) {
    return true; // rate limited
  }
  entry.count += 1;
  entry.lastRequest = now;
  rateLimitMap.set(ip, entry);
  return false;
} 