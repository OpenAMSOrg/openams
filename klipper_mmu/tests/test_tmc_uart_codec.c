// SPDX-License-Identifier: GPL-3.0-only
#include "klipper_mmu/tmc_uart_codec.h"
#include "klipper_mmu/tmc2209.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static void independent_frame(const uint8_t *raw, unsigned length, uint8_t *out)
{
    memset(out, 0, 10);
    /* Deliberately express the reference as wire bits, independently of the
     * two-byte windows used by the implementation. */
    for (unsigned byte = 0; byte < length; ++byte) {
        for (unsigned wire = 0; wire < 10u; ++wire) {
            unsigned value = wire == 9u ? 1u : wire == 0u ? 0u
                : ((unsigned)raw[byte] >> (wire - 1u)) & 1u;
            unsigned bit = 10u * byte + wire;
            out[bit / 8u] |= (uint8_t)(value << (bit % 8u));
        }
    }
}

int main(void)
{
    uint8_t raw[8], encoded[10], expected[10], decoded[8];
    unsigned cases = 0;
    for (unsigned length = 4; length <= 8; length += 4) {
        for (unsigned position = 0; position < length; ++position) {
            for (unsigned value = 0; value < 256; ++value) {
                memset(raw, 0xa5, sizeof(raw));
                raw[position] = (uint8_t)value;
                independent_frame(raw, length, expected);
                memset(encoded, 0x5a, sizeof(encoded));
                unsigned count = km_tmc_uart_encode(raw, (uint8_t)length, encoded);
                assert(count == length * 10u / 8u && !memcmp(encoded, expected, count));
                for (unsigned i = count; i < sizeof(encoded); ++i) assert(encoded[i] == 0x5a);
                if (length == 8u) {
                    assert(km_tmc_uart_decode(encoded, (uint8_t)count, decoded));
                    assert(!memcmp(raw, decoded, sizeof(raw)));
                }
                ++cases;
            }
        }
    }
    for (unsigned length = 0; length < 256; ++length) {
        if (length != 4u && length != 8u) {
            memset(encoded, 0x5a, sizeof(encoded));
            assert(!km_tmc_uart_encode(raw, (uint8_t)length, encoded));
            for (unsigned i = 0; i < sizeof(encoded); ++i) assert(encoded[i] == 0x5a);
        }
        if (length != 10u) {
            memset(decoded, 0x5a, sizeof(decoded));
            assert(!km_tmc_uart_decode(encoded, (uint8_t)length, decoded));
            for (unsigned i = 0; i < sizeof(decoded); ++i) assert(decoded[i] == 0x5a);
        }
    }
    assert(!km_tmc_uart_encode(NULL, 4, encoded));
    assert(!km_tmc_uart_encode(raw, 4, NULL));
    assert(!km_tmc_uart_decode(NULL, 10, decoded));
    assert(!km_tmc_uart_decode(encoded, 10, NULL));

    /* Real response composition: framing and protocol integrity are separate. */
    const uint8_t response[] = {0x05, 0xff, 0x6f, 0x12, 0x34, 0xab, 0xcd, 0};
    memcpy(raw, response, sizeof(raw));
    raw[7] = km_tmc2209_crc(raw, 7);
    assert(km_tmc_uart_encode(raw, 8, encoded) == 10);
    assert(km_tmc_uart_decode(encoded, 10, decoded));
    uint32_t value = 0;
    assert(km_tmc2209_decode(0x6f, decoded, 8, &value) == KM_OK && value == 0x1234abcd);
    for (unsigned bit = 0; bit < 80u; ++bit) {
        uint8_t corrupt[10];
        memcpy(corrupt, encoded, sizeof(corrupt));
        corrupt[bit / 8u] ^= (uint8_t)(1u << (bit % 8u));
        memset(decoded, 0x5a, sizeof(decoded));
        if (bit % 10u == 0u || bit % 10u == 9u) {
            assert(!km_tmc_uart_decode(corrupt, 10, decoded));
            for (unsigned i = 0; i < sizeof(decoded); ++i) assert(decoded[i] == 0x5a);
        } else {
            assert(km_tmc_uart_decode(corrupt, 10, decoded));
            assert(km_tmc2209_decode(0x6f, decoded, 8, &value) == KM_ERR_FAULTED);
        }
    }

    /* Exact and partial overlaps are safe because output is committed last. */
    for (unsigned offset = 0; offset < 8u; ++offset) {
        uint8_t overlap[24];
        memcpy(overlap, raw, 8);
        assert(km_tmc_uart_encode(overlap, 8, overlap + offset) == 10);
        assert(!memcmp(overlap + offset, encoded, 10));
        memcpy(overlap, encoded, 10);
        assert(km_tmc_uart_decode(overlap, 10, overlap + offset));
        assert(!memcmp(overlap + offset, raw, 8));
    }
    printf("tmc_uart_codec: %u framing cases, all 80 single-bit corruptions and overlap/invalid cases PASS\n", cases);
    return 0;
}
