// CloudFront Function (viewer request, site behaviour only). The static export writes
// app/index.html for /app/, but the S3 REST origin has no directory indexes, so map
// "/dir/" and "/dir" to "/dir/index.html". Paths with a file extension pass through.
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  if (uri.charAt(uri.length - 1) === "/") {
    request.uri = uri + "index.html";
  } else if (uri.lastIndexOf(".") < uri.lastIndexOf("/")) {
    request.uri = uri + "/index.html";
  }
  return request;
}
