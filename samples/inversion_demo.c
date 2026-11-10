/*
    * inversion demo -- Broken Variant
    * Demonstrates classic Mars Pathfinder priority inversion on qnx 8:
    * - logger_low (prio 10) locks shared mutex with PTHREAD_PRIO_NONE (No Inheritence)
    * - worker_med (prio 25) wakes up and preempts logger_low on CPU 0 for 25 ms.
    * - sensor_high (prio 40, deadline 10ms) wakes up, blocks on mutex (THMUTEX). Forces to wait for worker_med + logger_low (~30ms)
*/

#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include <time.h>
#include <sys/neutrino.h>
#include "aion_markers.h"

#ifndef MUTEX_PROTOCOL
#define MUTEX_PROTOCOL PTHREAD_PRIO_NONE
#endif

static pthread_mutex_t shared_lock;

static void burn_ms(double target_ms) {
    struct timespec s, n;
    clock_gettime(CLOCK_MONOTONIC, &s);
    while (1) {
        clock_gettime(CLOCK_MONOTONIC, &n);
        double el = (n.tv_sec - s.tv_sec) * 1000.0 + (n.tv_nsec - s.tv_nsec) / 1e6;
        if (el >= target_ms) break;
    }
}

static void* logger_low_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "logger_low");

    pthread_mutex_lock(&shared_lock);
    burn_ms(8.0); /* Holds lock for 8 ms of CPU work */
    pthread_mutex_unlock(&shared_lock);
    return NULL;
}

static void* worker_med_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "worker_med");

    usleep(2000);  /* Wait 2 ms so logger_low grabs mutex first */
    burn_ms(25.0); /* Hogs CPU 0 for 25 ms at priority 25! */
    return NULL;
}

static void* sensor_high_fn(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "sensor_high");

    /* Release 1: Healthy run before contention (takes ~1 ms, well under 10 ms deadline) */
    job_begin("sensor_high");
    burn_ms(1.0);
    job_end("sensor_high");

    usleep(4000); /* Sleep until t=5 ms (while logger_low holds lock and worker_med is running) */

    /* Release 2: Contended run! */
    job_begin("sensor_high");
    pthread_mutex_lock(&shared_lock);
    burn_ms(1.0);
    pthread_mutex_unlock(&shared_lock);
    job_end("sensor_high");
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

    pthread_mutexattr_t mattr;
    pthread_mutexattr_init(&mattr);
    pthread_mutexattr_setprotocol(&mattr, MUTEX_PROTOCOL);
    pthread_mutex_init(&shared_lock, &mattr);

    pthread_t t_low, t_med, t_high;
    spawn_rt(&t_low, 10, logger_low_fn);
    spawn_rt(&t_med, 25, worker_med_fn);
    spawn_rt(&t_high, 40, sensor_high_fn);

    pthread_join(t_high, NULL);
    pthread_join(t_med, NULL);
    pthread_join(t_low, NULL);
    return 0;
}