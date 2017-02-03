#include <stdio.h>
#include <string.h>
#include <pulse/pulseaudio.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <time.h>
#include <sys/types.h>
#include <sys/socket.h>

#define CLEAR_LINE "\n"
#define _(x) x

#define TIME_EVENT_USEC 50000

// From pulsecore/macro.h
#define pa_memzero(x,l) (memset((x), 0, (l)))
#define pa_zero(x) (pa_memzero(&(x), sizeof(x)))

int verbose = 1;
int ret;

pa_context *context;

static pa_sample_spec sample_spec = {
    PA_SAMPLE_S16LE,
    44100,
    2
};

static pa_stream *stream = NULL;

/* This is my builtin card. Use paman to find yours
   or set it to NULL to get the default device
*/
static char *device = NULL; //"alsa_input.pci-0000_00_1b.0.analog-stereo";

static int flags = 0; // pa_stream_flags_t

void stream_state_callback(pa_stream *s, void *userdata) {
    assert(s);
    switch (pa_stream_get_state(s)) {
        case PA_STREAM_CREATING:
            // The stream has been created, so
            // let's open a file to record to
            printf("Creating stream\n");
            //TODO FIXOR
            //fdout = creat(fname,  0711);
            break;
        case PA_STREAM_TERMINATED:
            //TODO FIXOR
            //close(fdout);
            break;
        case PA_STREAM_READY:
            // Just for info: no functionality in this branch
            if (verbose) {
                const pa_buffer_attr *a;
                char cmt[PA_CHANNEL_MAP_SNPRINT_MAX], sst[PA_SAMPLE_SPEC_SNPRINT_MAX];

                printf("Stream successfully created.");

                if (!(a = pa_stream_get_buffer_attr(s)))
                    printf("pa_stream_get_buffer_attr() failed: %s", pa_strerror(pa_context_errno(pa_stream_get_context(s))));
                else {
                    printf("Buffer metrics: maxlength=%u, fragsize=%u", a->maxlength, a->fragsize);

                }

                printf("Connected to device %s (%u, %ssuspended).",
                       pa_stream_get_device_name(s),
                       pa_stream_get_device_index(s),
                       pa_stream_is_suspended(s) ? "" : "not ");
            }
            break;
        case PA_STREAM_FAILED:
        default:
            printf("Stream error: %s", pa_strerror(pa_context_errno(pa_stream_get_context(s))));
            exit(1);
    }
}

long long latency_usec;

/* Show the current latency */
static void stream_update_timing_callback(pa_stream *s, int success, void *userdata) {
    pa_usec_t l, usec;
    int negative = 0;

    // pa_assert(s);

    if (!success ||
            pa_stream_get_time(s, &usec) < 0 ||
            pa_stream_get_latency(s, &l, &negative) < 0) {
        // pa_log(_("Failed to get latency"));
        //pa_log(_("Failed to get latency: %s"), pa_strerror(pa_context_errno(context)));
        // quit(1);
        return;
    }

    if(negative)
        latency_usec = -l;
    else
        latency_usec = l;

    fprintf(stderr, _("Time: %0.3f sec; Latency: %0.0f usec.\n"),
            (float) usec / 1000000,
            (float) l * (negative?-1.0f:1.0f));
}

static void time_event_callback(pa_mainloop_api *m, pa_time_event *e, const struct timeval *t, void *userdata) {
    if (stream && pa_stream_get_state(stream) == PA_STREAM_READY) {
        pa_operation *o;
        if (!(o = pa_stream_update_timing_info(stream, stream_update_timing_callback, NULL)))
            fprintf(stderr, "pa_stream_update_timing_info() failed: %s", pa_strerror(pa_context_errno(context)));
        else
            pa_operation_unref(o);
    }

    pa_context_rttime_restart(context, e, pa_rtclock_now() + TIME_EVENT_USEC);
}


void get_latency(pa_stream *s) {
    pa_usec_t latency;
    int neg;
    const pa_timing_info *timing_info;

    timing_info = pa_stream_get_timing_info(s);

    if (pa_stream_get_latency(s, &latency, &neg) != 0) {
        fprintf(stderr, __FILE__": pa_stream_get_latency() failed\n");
        return;
    }

    fprintf(stderr, "%0.0f usec    \r", (float)latency);
}


/*********** Stream callbacks **************/
char buf[4098];
int buffill = 0;
int buflen = 1004;

struct sockaddr_in addr;
int addrlen, sock, cnt;
int packet_counter = 1234;
int byte_counter = 1234;

int min(int a, int b) {
    return a < b ? a : b;
}

/* This is called whenever new data is available */
static void stream_read_callback(pa_stream *s, size_t length, void *userdata) {
    assert(s);
    assert(length > 0);

    while (pa_stream_readable_size(s) > 0) {
        const void *data;
        size_t length;

        // peek actually creates and fills the data vbl
        if (pa_stream_peek(s, &data, &length) < 0) {
            fprintf(stderr, "Read failed\n");
            exit(1);
            return;
        }
        //sprintf("sending2: %d\n", length);

        int used = 0;
        while(used < length) {
            int gonnause = min(buflen-buffill, length-used);
            memcpy(buf+28+buffill, ((unsigned char*)data)+used, gonnause);
            buffill += gonnause;
            used += gonnause;
            if(buffill == buflen) {
                buffill = 0;

                timespec tm;
                clock_gettime(CLOCK_MONOTONIC, &tm);
                long long nsec = tm.tv_nsec;
                long long sec = tm.tv_sec;
                nsec -= latency_usec * 1000;
                //printf("latency_usec: %d\n", latency_usec);
                nsec += 50 * 1000000;
                while(nsec < 0) {
                    nsec += 1e9;
                    sec--;
                }
                while(nsec >= 1e9) {
                    nsec -= 1e9;
                    sec++;
                }

                * (unsigned int*) (buf+0) = htonl(packet_counter);
                * (unsigned int*) (buf+4) = 0;
                * (unsigned int*) (buf+8) = 0xf0030001;
                * (unsigned int*) (buf+12) = htonl(sec);
                * (unsigned int*) (buf+16) = htonl(nsec/1000);
                * (unsigned int*) (buf+20) =  htonl(byte_counter);
                * (unsigned short*) (buf+24) =  0x1002;
                * (unsigned short*) (buf+26) =  htons(44100);
                byte_counter += buflen;
                packet_counter += 1;

                int cnt = sendto(sock, buf, 28+buflen, 0, (struct sockaddr *) &addr, addrlen);
                if (cnt < 0) {
                    perror("sendto");
                    exit(1);
                }
            }
        }


//        if(cb != NULL)
//            cb((short*) data, length/4);
//        fprintf(stderr, "Writing %d\n", length);
//        write(fdout, data, length);

        // swallow the data peeked at before
        pa_stream_drop(s);
    }
}


// This callback gets called when our context changes state.  We really only
// care about when it's ready or if it has failed
void state_cb(pa_context *c, void *userdata) {
    pa_context_state_t state;
    int *pa_ready = (int*) userdata;

    printf("State changed\n");
    state = pa_context_get_state(c);
    switch  (state) {
        // There are just here for reference
        case PA_CONTEXT_UNCONNECTED:
        case PA_CONTEXT_CONNECTING:
        case PA_CONTEXT_AUTHORIZING:
        case PA_CONTEXT_SETTING_NAME:
        default:
            break;
        case PA_CONTEXT_FAILED:
        case PA_CONTEXT_TERMINATED:
            *pa_ready = 2;
            break;
        case PA_CONTEXT_READY: {
            pa_buffer_attr buffer_attr;

            if (verbose)
                printf("Connection established.%s\n", CLEAR_LINE);

            if (!(stream = pa_stream_new(c, "SonosCast", &sample_spec, NULL))) {
                printf("pa_stream_new() failed: %s", pa_strerror(pa_context_errno(c)));
                exit(1);
            }

            // Watch for changes in the stream state to create the output file
            pa_stream_set_state_callback(stream, stream_state_callback, NULL);

            // Watch for changes in the stream's read state to write to the output file
            pa_stream_set_read_callback(stream, stream_read_callback, NULL);

            // timing info
            pa_stream_update_timing_info(stream, stream_update_timing_callback, NULL);

            // Set properties of the record buffer
            pa_zero(buffer_attr);
            buffer_attr.maxlength = buflen;
            buffer_attr.prebuf = 0;

            buffer_attr.fragsize = buffer_attr.tlength = buflen;
            flags |= PA_STREAM_ADJUST_LATENCY;

            buffer_attr.minreq = (uint32_t) buflen;

            flags |= PA_STREAM_INTERPOLATE_TIMING;

            // and start recording
            if (pa_stream_connect_record(stream, device, &buffer_attr, (pa_stream_flags_t)flags) < 0) {
                printf("pa_stream_connect_record() failed: %s", pa_strerror(pa_context_errno(c)));
                exit(1);
            }
            break;
        }
    }
}
int main() {

    /* set up socket */
    sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) {
        perror("socket");
        exit(1);
    }
    bzero((char *)&addr, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(6982);
    addrlen = sizeof(addr);

    /* send */
    addr.sin_addr.s_addr = inet_addr("225.238.76.46");

    // Define our pulse audio loop and connection variables
    pa_mainloop *pa_ml;
    pa_mainloop_api *pa_mlapi;
    pa_operation *pa_op;
    pa_time_event *time_event;

    // Create a mainloop API and connection to the default server
    pa_ml = pa_mainloop_new();
    pa_mlapi = pa_mainloop_get_api(pa_ml);
    context = pa_context_new(pa_mlapi, "test");

    // This function connects to the pulse server
    pa_context_connect(context, NULL, (pa_context_flags_t)0, NULL);

    // This function defines a callback so the server will tell us its state.
    pa_context_set_state_callback(context, state_cb, NULL);

    if (!(time_event = pa_context_rttime_new(context, pa_rtclock_now() + TIME_EVENT_USEC, time_event_callback, NULL))) {
        printf("pa_mainloop_run() failed.");
        exit(1);
    }


    if (pa_mainloop_run(pa_ml, &ret) < 0) {
        printf("pa_mainloop_run() failed.");
        exit(1);
    }
}
