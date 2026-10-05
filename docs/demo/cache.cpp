#include <cstdint>
#include <map>
#include <mutex>
#include <string>

/// A TTL cache, shared by readers and a cleanup thread.
class Cache
{
public:
    explicit Cache(int64_t ttl_) : ttl(ttl_) {}

    /// Returns the value and extends its lifetime.
    std::string get(const std::string & key)
    {
        std::lock_guard lock(mutex);
        auto it = entries.find(key);
        if (it == entries.end())
            return {};
        refresh(key);
        return it->second.value;
    }

    void put(const std::string & key, std::string value, int64_t now)
    {
        std::lock_guard lock(mutex);
        entries[key] = Entry{std::move(value), now + ttl};
    }

    /// Called by the cleanup thread.
    void cleanupExpired(int64_t now)
    {
        std::lock_guard lock(mutex);
        for (auto it = entries.begin(); it != entries.end();)
        {
            if (it->second.expires_at < now)
                it = entries.erase(it);
            else
                ++it;
        }
    }

private:
    struct Entry
    {
        std::string value;
        int64_t expires_at = 0;
    };

    /// Moves the node out of the map and back in with a new expiry time.
    void refresh(const std::string & key)
    {
        auto node = entries.extract(key);
        node.mapped().expires_at += ttl;
        entries.insert(std::move(node));
    }

    const int64_t ttl;
    std::mutex mutex;
    std::map<std::string, Entry> entries;
};
