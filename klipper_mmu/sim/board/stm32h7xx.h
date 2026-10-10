// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef __STM32H7XX_H
#define __STM32H7XX_H

#define GPIOA (void*)1
#define GPIOB (void*)2
#define GPIOC (void*)3
#define GPIOD (void*)4
#define GPIOE (void*)5
#define GPIOF (void*)6
#define GPIOG (void*)7
#define GPIOH (void*)8
#define GPIOI (void*)9

typedef struct {
    uint32_t MODER;
    uint32_t OTYPER;
    uint32_t OSPEEDR;
    uint32_t PUPDR;
    uint32_t IDR;
    uint32_t ODR;
    uint32_t BSRR;
    uint32_t LCKR;
    uint32_t AFR[2];
} GPIO_TypeDef;

// Stub for RNG struct
typedef struct {
    uint32_t CR;
    uint32_t SR;
    uint32_t DR;
    uint32_t HTCR;
} RNG_TypeDef;

#define RNG ((RNG_TypeDef*)1)
#define RCC ((void*)1)

#endif
