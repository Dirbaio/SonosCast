stream: stream.cpp rtkit.c rtkit.h realtime.cpp realtime.h sntp.cpp sntp.h
	g++ \
		stream.cpp rtkit.c realtime.cpp sntp.cpp \
		-std=c++11 \
		-o stream \
		-lpulse -lpthread \
		-O3 \
		`pkg-config --cflags dbus-1` \
    	`pkg-config --libs dbus-1` \
		-DHAVE_DBUS \
		-DHAVE_SCHED_H
