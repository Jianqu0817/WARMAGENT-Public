// Pure overlap scoring over Python-normalized Unicode codepoints.
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#if defined(_WIN32)
#define WARM_EXPORT __declspec(dllexport)
#else
#define WARM_EXPORT __attribute__((visibility("default")))
#endif

struct Grams {
    uint64_t* keys;
    size_t capacity;
    size_t size;
};

static size_t slot(uint64_t value, size_t capacity) {
    value ^= value >> 30;
    value *= UINT64_C(0xbf58476d1ce4e5b9);
    value ^= value >> 27;
    return size_t(value) & (capacity - 1);
}

static Grams grams(const uint32_t* text, size_t length) {
    size_t capacity = 2;
    while (capacity < length * 2) capacity *= 2;
    Grams result{static_cast<uint64_t*>(calloc(capacity, sizeof(uint64_t))), capacity, 0};
    if (!result.keys) return result;
    for (size_t i = 1; i < length; ++i) {
        // Python codepoints are <= 0x10ffff, so +1 cannot overflow and
        // preserves U+0000 pairs while reserving zero as the empty slot.
        const uint64_t key = ((uint64_t(text[i - 1]) << 32) | text[i]) + 1;
        size_t index = slot(key, capacity);
        while (result.keys[index] && result.keys[index] != key) index = (index + 1) & (capacity - 1);
        if (!result.keys[index]) { result.keys[index] = key; ++result.size; }
    }
    return result;
}

static bool contains(const Grams& set, uint64_t key) {
    size_t index = slot(key, set.capacity);
    while (set.keys[index] && set.keys[index] != key) index = (index + 1) & (set.capacity - 1);
    return set.keys[index] == key;
}

extern "C" WARM_EXPORT int warm_overlap_batch(
    const uint32_t* query, size_t query_length,
    const uint32_t* texts, const size_t* offsets,
    size_t count, double* scores) noexcept {
    const auto left = grams(query, query_length);
    if (!left.keys) return 1;
    for (size_t i = 0; i < count; ++i) {
        if (!left.size) { scores[i] = 0; continue; }
        const size_t length = offsets[i + 1] - offsets[i];
        const auto right = grams(texts + offsets[i], length);
        if (!right.keys) { free(left.keys); return 1; }
        const auto& smaller = left.capacity < right.capacity ? left : right;
        const auto& larger = left.capacity < right.capacity ? right : left;
        size_t shared = 0;
        for (size_t j = 0; j < smaller.capacity; ++j) {
            if (smaller.keys[j]) shared += contains(larger, smaller.keys[j]);
        }
        const size_t denominator = left.size < right.size ? left.size : right.size;
        scores[i] = double(shared) / (denominator ? denominator : 1);
        free(right.keys);
    }
    free(left.keys);
    return 0;
}

