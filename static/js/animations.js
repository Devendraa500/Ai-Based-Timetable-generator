/* ============================================================
   SMART TIMETABLE ASSISTANT — ANIMATION CONTROLLER
   GSAP-powered reveals, magnetic cursor, page transitions
   Inspired by beui.dev, landonorris.com, details.so
   ============================================================ */

(function () {
    'use strict';

    /* ─── Wait for GSAP ─── */
    function initAnimations() {
        if (typeof gsap === 'undefined') {
            console.warn('[animations] GSAP not loaded, skipping animations.');
            // Still reveal elements so they aren't invisible
            document.querySelectorAll('.reveal-up, .reveal-fade, .reveal-left, .reveal-scale').forEach(el => {
                el.style.opacity = '1';
                el.style.transform = 'none';
            });
            return;
        }

        /* ─── Staggered Reveal on Load ─── */
        gsap.defaults({ ease: 'power3.out', duration: 0.8 });

        // Reveal-up elements
        const revealUp = document.querySelectorAll('.reveal-up');
        if (revealUp.length) {
            gsap.to(revealUp, {
                opacity: 1,
                y: 0,
                stagger: 0.08,
                delay: 0.15,
                duration: 0.7,
            });
        }

        // Reveal-fade elements
        const revealFade = document.querySelectorAll('.reveal-fade');
        if (revealFade.length) {
            gsap.to(revealFade, {
                opacity: 1,
                stagger: 0.06,
                delay: 0.2,
                duration: 0.6,
            });
        }

        // Reveal-left elements
        const revealLeft = document.querySelectorAll('.reveal-left');
        if (revealLeft.length) {
            gsap.to(revealLeft, {
                opacity: 1,
                x: 0,
                stagger: 0.08,
                delay: 0.15,
                duration: 0.7,
            });
        }

        // Reveal-scale elements
        const revealScale = document.querySelectorAll('.reveal-scale');
        if (revealScale.length) {
            gsap.to(revealScale, {
                opacity: 1,
                scale: 1,
                stagger: 0.08,
                delay: 0.15,
                duration: 0.7,
            });
        }

        /* ─── Magnetic Cursor Effect (desktop only) ─── */
        if (window.matchMedia('(hover: hover) and (pointer: fine)').matches) {
            const magneticEls = document.querySelectorAll('.magnetic');
            magneticEls.forEach(el => {
                el.addEventListener('mousemove', (e) => {
                    const rect = el.getBoundingClientRect();
                    const x = e.clientX - rect.left - rect.width / 2;
                    const y = e.clientY - rect.top - rect.height / 2;
                    gsap.to(el, {
                        x: x * 0.15,
                        y: y * 0.15,
                        duration: 0.3,
                        ease: 'power2.out',
                    });
                });

                el.addEventListener('mouseleave', () => {
                    gsap.to(el, {
                        x: 0,
                        y: 0,
                        duration: 0.5,
                        ease: 'elastic.out(1, 0.5)',
                    });
                });
            });
        }

        /* ─── Intersection Observer for scroll-triggered reveals ─── */
        if ('IntersectionObserver' in window) {
            const scrollRevealEls = document.querySelectorAll('[data-scroll-reveal]');
            if (scrollRevealEls.length) {
                const observer = new IntersectionObserver((entries) => {
                    entries.forEach(entry => {
                        if (entry.isIntersecting) {
                            const el = entry.target;
                            const delay = parseFloat(el.dataset.scrollDelay || 0);
                            gsap.to(el, {
                                opacity: 1,
                                y: 0,
                                x: 0,
                                scale: 1,
                                delay: delay,
                                duration: 0.7,
                            });
                            observer.unobserve(el);
                        }
                    });
                }, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });

                scrollRevealEls.forEach(el => observer.observe(el));
            }
        }

        /* ─── Nav link hover animations ─── */
        const navLinks = document.querySelectorAll('.nav-link');
        navLinks.forEach(link => {
            link.addEventListener('mouseenter', () => {
                gsap.to(link, {
                    scale: 1.05,
                    duration: 0.2,
                    ease: 'power2.out',
                });
            });
            link.addEventListener('mouseleave', () => {
                gsap.to(link, {
                    scale: 1,
                    duration: 0.3,
                    ease: 'power2.out',
                });
            });
        });

        /* ─── Card hover effects ─── */
        const cards = document.querySelectorAll('.card-premium');
        cards.forEach(card => {
            card.addEventListener('mouseenter', () => {
                gsap.to(card, {
                    y: -4,
                    duration: 0.3,
                    ease: 'power2.out',
                });
            });
            card.addEventListener('mouseleave', () => {
                gsap.to(card, {
                    y: 0,
                    duration: 0.4,
                    ease: 'power2.out',
                });
            });
        });
    }

    /* ─── Hero Cursor Reveal (Login Page) ─── */
    function initHeroReveal() {
        const hero = document.querySelector('.hero-login');
        const revealLayer = document.querySelector('.hero-layer-gold-reveal');

        if (!hero || !revealLayer) return;

        // Only on desktop with precise pointer
        if (!window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;

        let targetX = 0, targetY = 0;
        let currentX = 0, currentY = 0;
        let isFirstMove = true;
        let rafId = null;
        let isActive = false;
        const LERP_FACTOR = 0.22;

        function lerp(a, b, t) {
            return a + (b - a) * t;
        }

        function updatePosition() {
            if (!isActive) {
                rafId = null;
                return;
            }

            currentX = lerp(currentX, targetX, LERP_FACTOR);
            currentY = lerp(currentY, targetY, LERP_FACTOR);

            const maskX = currentX - 80;
            const maskY = currentY - 100;

            revealLayer.style.maskPosition = `${maskX}px ${maskY}px`;
            revealLayer.style.webkitMaskPosition = `${maskX}px ${maskY}px`;

            const dx = Math.abs(targetX - currentX);
            const dy = Math.abs(targetY - currentY);

            if (dx < 0.3 && dy < 0.3) {
                currentX = targetX;
                currentY = targetY;
                // Don't stop — wait for next move or mouseleave
            }

            rafId = requestAnimationFrame(updatePosition);
        }

        hero.addEventListener('mousemove', (e) => {
            const rect = hero.getBoundingClientRect();
            targetX = e.clientX - rect.left;
            targetY = e.clientY - rect.top;

            if (isFirstMove) {
                currentX = targetX;
                currentY = targetY;
                isFirstMove = false;
                revealLayer.style.opacity = '1';
            }

            if (!isActive) {
                isActive = true;
                if (!rafId) {
                    rafId = requestAnimationFrame(updatePosition);
                }
            }
        });

        hero.addEventListener('mouseleave', () => {
            isActive = false;
            revealLayer.style.opacity = '0';
            isFirstMove = true;
            if (rafId) {
                cancelAnimationFrame(rafId);
                rafId = null;
            }
        });
    }

    /* ─── Theme Toggle ─── */
    function initTheme() {
        const btn = document.getElementById('themeToggleBtn');
        if (!btn) return;

        function syncTheme() {
            const isLight = document.body.classList.contains('light');
            btn.textContent = isLight ? '◐ dark' : '◑ light';
        }

        function toggleTheme() {
            document.body.classList.toggle('light');
            const theme = document.body.classList.contains('light') ? 'light' : 'dark';
            localStorage.setItem('theme', theme);
            syncTheme();
        }

        // Restore saved theme
        if (localStorage.getItem('theme') === 'light') {
            document.body.classList.add('light');
        }

        syncTheme();
        btn.addEventListener('click', toggleTheme);
    }

    /* ─── Active Nav Link ─── */
    function initActiveNav() {
        const currentPath = window.location.pathname;
        document.querySelectorAll('.nav-link').forEach(link => {
            if (link.getAttribute('href') === currentPath) {
                link.classList.add('active');
            }
        });
    }

    /* ─── Init ─── */
    function init() {
        initTheme();
        initActiveNav();
        initAnimations();
        initHeroReveal();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
