#include <stdio.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include <time.h>
#include "aion_markers.h"

#ifndef HOG_PRIO
#define HOG_PRIO 45 /* Bug: background hog configured higher than actuator_high (40)! */
#endif

static void burn_ms(double target_ms) {
    struct timespec s, n;
    clock_gettime(CLOCK_MONOTONIC, &s);
    while (1) {
        clock_gettime(CLOCK_MONOTONIC, &n);
        double el = (n.tv_sec - s.tv_sec) * 1000.0 + (n.tv_nsec - s.tv_nsec) / 1e6;
        if (el >= target_ms) break;
    }
}

static void* hog_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "telemetry_hog");
    usleep(2000);
    burn_ms(20.0); /* Starves CPU 0 for 20 ms! */
    return NULL;
}

static void* actuator_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "actuator_high");
    job_begin("actuator_high");
    burn_ms(1.0);
    job_end("actuator_high");

    usleep(4000); /* Wakes up at 5 ms while telemetry_hog owns CPU 0 */
    job_begin("actuator_high");
    burn_ms(1.5);
    job_end("actuator_high");
    return NULL;
}

static void spawn_rt(pthread_t* t, int prio, void* (*fn)(void*)) {
    pthread_attr_t attr;
    struct sched_param param;
    pthread_attr_init(&attr);
    pthread_attr_setinheritsched(&attr, PTHREAD_EXPLICIT_SCHED);
    pthread_attr_setschedpolicy(&attr, SCHED_FIFO);
    param.sched_priority = prio;
    pthread_attr_setschedparam(&attr, &param);
    pthread_create(t, &attr, fn, NULL);
}

int main(void) {
    pin_to_cpu0();
    trace_init();
    struct sched_param sp = { .sched_priority = 55 };
    sched_setscheduler(0, SCHED_FIFO, &sp);

    pthread_t th_hog, th_act;
    spawn_rt(&th_hog, HOG_PRIO, hog_fn);
    spawn_rt(&th_act, 40, actuator_fn);

    pthread_join(th_act, NULL);
    pthread_join(th_hog, NULL);
    return 0;
}