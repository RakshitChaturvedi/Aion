#include <stdio.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#include <time.h>
#include "aion_markers.h"

static pthread_mutex_t shared_mutex;

static void burn_ms(double target_ms) {
    struct timespec s, n;
    clock_gettime(CLOCK_MONOTONIC, &s);
    while (1) {
        clock_gettime(CLOCK_MONOTONIC, &n);
        double el = (n.tv_sec - s.tv_sec) * 1000.0 + (n.tv_nsec - s.tv_nsec) / 1e6;
        if (el >= target_ms) break;
    }
}

static void* low_holder(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "diag_low");
    pthread_mutex_lock(&shared_mutex);
    burn_ms(8.0);
    pthread_mutex_unlock(&shared_mutex);
    return NULL;
}

static void* med_preemptor(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "crunch_med");
    usleep(2000);
    burn_ms(22.0);
    return NULL;
}

static void* high_waiter(void* arg) {
    (void)arg;
    pin_to_cpu0();
    pthread_setname_np(pthread_self(), "control_high");
    job_begin("control_high");
    burn_ms(1.0);
    job_end("control_high");

    usleep(4000);
    job_begin("control_high");
    pthread_mutex_lock(&shared_mutex);
    burn_ms(1.0);
    pthread_mutex_unlock(&shared_mutex);
    job_end("control_high");
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
    pthread_mutexattr_setprotocol(&mattr, PTHREAD_PRIO_INHERIT);
    pthread_mutex_init(&shared_mutex, &mattr);

    pthread_t t1, t2, t3;
    spawn_rt(&t1, 10, low_holder);
    spawn_rt(&t2, 25, med_preemptor);
    spawn_rt(&t3, 40, high_waiter);

    pthread_join(t3, NULL);
    pthread_join(t2, NULL);
    pthread_join(t1, NULL);
    return 0;
}