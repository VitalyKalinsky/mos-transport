FROM nginx:1.27-alpine
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf
COPY dashboard/index.html dashboard/app.js dashboard/styles.css /usr/share/nginx/html/
COPY dashboard/vendor /usr/share/nginx/html/vendor
