document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.sentence').forEach((sentence) => {
    const toggle = () => {
      const translation = sentence.querySelector('.translation');
      translation.hidden = !translation.hidden;
      sentence.setAttribute('aria-expanded', String(!translation.hidden));
    };
    sentence.addEventListener('click', toggle);
    sentence.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        toggle();
      }
    });
  });
});
