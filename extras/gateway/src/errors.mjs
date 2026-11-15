export class ApiError extends Error {
  constructor(code, status = 400, outcomeUnknown = false) {
    super(code);
    this.code = code;
    this.status = status;
    this.outcomeUnknown = outcomeUnknown;
  }
}
