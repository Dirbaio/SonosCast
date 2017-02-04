#include "rtkit.h"
#include <string.h>
#include <pthread.h>
#include <stdio.h>
#include <errno.h>
#include <sys/time.h>
#include <sys/resource.h>

#ifdef HAVE_SCHED_H
#include <sched.h>

#if defined(__linux__) && !defined(SCHED_RESET_ON_FORK)
#define SCHED_RESET_ON_FORK 0x40000000
#endif
#endif

static int set_scheduler(int rtprio) {
#ifdef HAVE_SCHED_H
    struct sched_param sp;
#ifdef HAVE_DBUS
    int r;
    long long rttime;
#ifdef RLIMIT_RTTIME
    struct rlimit rl;
#endif
    DBusError error;
    DBusConnection *bus;

    dbus_error_init(&error);
#endif

    memset(&sp, 0, sizeof(sp));
    sp.sched_priority = rtprio;

#ifdef SCHED_RESET_ON_FORK
    if (pthread_setschedparam(pthread_self(), SCHED_RR|SCHED_RESET_ON_FORK, &sp) == 0) {
        printf("SCHED_RR|SCHED_RESET_ON_FORK worked.\n");
        return 0;
    }
#endif

    if (pthread_setschedparam(pthread_self(), SCHED_RR, &sp) == 0) {
        printf("SCHED_RR worked.\n");
        return 0;
    }
#endif  /* HAVE_SCHED_H */

#ifdef HAVE_DBUS
    /* Try to talk to RealtimeKit */

    if (!(bus = dbus_bus_get_private(DBUS_BUS_SYSTEM, &error))) {
        printf("Failed to connect to system bus: %s\n", error.message);
        dbus_error_free(&error);
        errno = -EIO;
        return -1;
    }

    /* We need to disable exit on disconnect because otherwise
     * dbus_shutdown will kill us. See
     * https://bugs.freedesktop.org/show_bug.cgi?id=16924 */
    dbus_connection_set_exit_on_disconnect(bus, FALSE);

    rttime = rtkit_get_rttime_usec_max(bus);
    if (rttime >= 0) {
#ifdef RLIMIT_RTTIME
        r = getrlimit(RLIMIT_RTTIME, &rl);

        if (rl.rlim_max > (rlim_t) rttime) {
            printf("Clamping rlimit-rttime to %lld for RealtimeKit\n", rttime);
            rl.rlim_cur = rl.rlim_max = rttime;
            r = setrlimit(RLIMIT_RTTIME, &rl);

            if (r < 0)
                printf("setrlimit() failed: %s\n", strerror(errno));
        }
#endif
        r = rtkit_make_realtime(bus, 0, rtprio);
        dbus_connection_close(bus);
        dbus_connection_unref(bus);

        if (r >= 0) {
            printf("RealtimeKit worked.\n");
            return 0;
        }
        printf("RealtimeKit error %i.\n", r);

        errno = -r;
    } else {
        dbus_connection_close(bus);
        dbus_connection_unref(bus);
        errno = -rttime;
    }
#endif
    return -1;
}


/* Make the current thread a realtime thread, and acquire the highest
 * rtprio we can get that is less or equal the specified parameter. If
 * the thread is already realtime, don't do anything. */
int make_realtime(int rtprio) {
    int p;

    if (set_scheduler(rtprio) >= 0) {
        printf("Successfully enabled SCHED_RR scheduling for thread, with priority %i.\n", rtprio);
        return 0;
    }

    for (p = rtprio-1; p >= 1; p--)
        if (set_scheduler(p) >= 0) {
            printf("Successfully enabled SCHED_RR scheduling for thread, with priority %i, which is lower than the requested %i.\n", p, rtprio);
            return 0;
        }

    printf("Failed to acquire real-time scheduling: %s\n", strerror(errno));
    printf("This is fine, but audio may stutter if you have high CPU usage\n");
    return -1;
}
