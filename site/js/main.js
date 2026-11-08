// Client-side only: no backend. Each feature runs only on the page that has its elements.
(function () {
  "use strict";

  // ---------- Build a Bowl (builder.html) ----------
  var builder = document.getElementById("builder");
  if (builder) {
    var summary = document.getElementById("summary");
    var richText = document.getElementById("richness-text");
    var fill = document.getElementById("meter-fill");
    var MAX = 4 + 5; // richest broth + a loaded set of toppings

    function listToText(items) {
      if (items.length === 0) return "no toppings";
      if (items.length === 1) return items[0];
      return items.slice(0, -1).join(", ") + " and " + items[items.length - 1];
    }

    function update() {
      var broth = builder.querySelector('input[name="broth"]:checked');
      var noodle = builder.querySelector('input[name="noodle"]:checked');
      var toppings = Array.prototype.slice.call(
        builder.querySelectorAll('input[name="topping"]:checked'));

      var score = Number(broth.dataset.rich);
      toppings.forEach(function (t) { score += Number(t.dataset.rich); });
      score = Math.min(score, MAX);

      summary.textContent = "A bowl of " + broth.value + " ramen with " + noodle.value +
        " noodles, topped with " + listToText(toppings.map(function (t) { return t.value; })) + ".";

      var pct = Math.round((score / MAX) * 100);
      var label = pct < 35 ? "Light" : pct < 65 ? "Medium" : "Heavy";
      richText.textContent = label + " (" + pct + "%)";
      fill.style.width = pct + "%";
    }

    builder.addEventListener("change", update);
    document.getElementById("reset").addEventListener("click", function () {
      builder.reset();
      update();
    });
    update();
  }

  // ---------- Contact form validation (about.html) ----------
  var form = document.getElementById("contact-form");
  if (form) {
    var status = document.getElementById("form-status");

    function setError(id, msg) {
      document.getElementById(id + "-error").textContent = msg;
      document.getElementById(id).setAttribute("aria-invalid", msg ? "true" : "false");
      return !msg;
    }

    form.addEventListener("submit", function (e) {
      e.preventDefault();
      status.textContent = "";
      var name = form.name.value.trim();
      var email = form.email.value.trim();
      var message = form.message.value.trim();

      var ok = true;
      ok = setError("name", name.length < 2 ? "Please enter your name." : "") && ok;
      ok = setError("email", /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ? "" : "Please enter a valid email address.") && ok;
      ok = setError("message", message.length < 10 ? "Your message must be at least 10 characters." : "") && ok;

      if (ok) {
        status.textContent = "Thanks, " + name + "! Your message looks good. (Demo form: nothing was sent.)";
        form.reset();
      }
    });
  }
})();
