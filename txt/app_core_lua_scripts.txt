TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local capacity, rate, now = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(bucket[1]) or capacity
local last_refill = tonumber(bucket[2]) or now
tokens = math.min(capacity, tokens + (math.max(0, now - last_refill) * rate))
if tokens >= 1 then
    tokens = tokens - 1
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    return {1, tokens}
else
    return {0, (1 - tokens) / rate}
end
"""
