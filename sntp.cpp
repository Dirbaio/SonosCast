#include <endian.h>    // __BYTE_ORDER
#include <algorithm>   // std::reverse
#include<stdio.h>
#include<string.h>
#include<stdlib.h>
#include<arpa/inet.h>
#include<sys/socket.h>
#include <time.h>
#include<unistd.h>

#define BUFLEN 512
#define PORT 12300
#define UNIX_TO_NTP_OFFSET 2208988800LL

/**
 * timestamps are in 2^32th's of seconds - 8 bytes, big endian
 *                                                  24                32                40
 * Request:                                                                             request send time
 * 1b0f08000000000000000000000000000000000000000000 00000000 00000000 00000000 00000000 83aa9b18 23309800
 * Response:                                        req send time     req recv time     resp send time
 * 1c0f08000000000000000000000000000000000000000000 83aa9b18 23309800 83aaeb55 6d333800 83aaeb55 6d3ff000
 */

void die(const char *s)
{
    perror(s);
    exit(1);
}

template <typename T>
T htont (T value) noexcept
{
#if __BYTE_ORDER == __LITTLE_ENDIAN
    char* ptr = reinterpret_cast<char*>(&value);
    std::reverse (ptr, ptr + sizeof(T));
#endif
    return value;
}

struct SNTPPacket {
    int header;
    int zero1;
    int zero2;
    int zero3;
    int zero4;
    int zero5;
    long long req_send_time;
    long long req_recv_time;
    long long send_time;
};

void sntp_loop() {
    struct sockaddr_in si_me, si_other;

    int s, i;
    socklen_t slen = sizeof(si_other);

    SNTPPacket packet;

    //create a UDP socket
    if ((s=socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)) == -1)
    {
        die("socket");
    }

    // zero out the structure
    memset((char *) &si_me, 0, sizeof(si_me));

    si_me.sin_family = AF_INET;
    si_me.sin_port = htons(PORT);
    si_me.sin_addr.s_addr = htonl(INADDR_ANY);

    //bind socket to port
    if( bind(s , (struct sockaddr*)&si_me, sizeof(si_me) ) == -1)
        die("bind");

    //keep listening for data
    while(1) {
        printf("Waiting for data...\n");

        int recv_len = recvfrom(s, &packet, sizeof(packet), 0, (struct sockaddr *) &si_other, &slen);

        //try to receive some data, this is a blocking call
        if(recv_len == -1)
            die("recvfrom()");
        //print details of the client/vpeer and the data received
        printf("Received packet from %s:%d\n", inet_ntoa(si_other.sin_addr), ntohs(si_other.sin_port));
        if(recv_len != sizeof(packet))
            printf("Unexpected length, got %d, should be %d\n", recv_len, sizeof(packet));

        timespec tm;
        clock_gettime(CLOCK_MONOTONIC, &tm);
        long long t = ((long long)(tm.tv_nsec) << 32) / 1000000000;
        t += (long long)(tm.tv_sec + UNIX_TO_NTP_OFFSET) << 32;
        packet.header = 0x0f1c;
        packet.req_send_time = packet.send_time;
        packet.req_recv_time = packet.send_time = htont(t);

        //now reply the client with the same data
        if (sendto(s, &packet, sizeof(packet), 0, (struct sockaddr*) &si_other, slen) == -1)
            die("sendto()");
    }

    close(s);
}
