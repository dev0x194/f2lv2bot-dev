FROM nginx:alpine AS build
RUN apk add --no-cache gcc musl-dev linux-headers
COPY no-egress.c /src/no-egress.c
RUN cc -O2 -Wall -Wextra -Werror /src/no-egress.c -o /no-egress

FROM nginx:alpine
COPY --from=build /no-egress /usr/local/bin/no-egress
USER 101:101
ENTRYPOINT ["/usr/local/bin/no-egress"]
CMD ["nginx", "-c", "/etc/nginx/ad-documents.conf", "-g", "daemon off;"]
