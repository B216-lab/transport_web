pipeline {
    agent any

    environment {
        // Путь к проекту на Jenkins-агенте (обычно автоматический, но можно задать)
        // COMPOSE_FILE = 'docker-compose.yml'
    }

    stages {
        stage('Checkout') {
            steps {
                // Клонируем репозиторий с GitHub (замените на ваш URL, если нужно)
                // Если репозиторий публичный, хватит и этого.
                // Если приватный, добавьте credentialsId: 'github-credentials'
                git url: 'https://github.com/B216-lab/transport_web.git',
                    branch: 'main'
            }
        }

        stage('Build Docker Images') {
            steps {
                echo 'Сборка Docker образов...'
                // Собираем образ бэкенда из папки backend
                sh 'docker build -t transport-web-backend ./backend'
                // Собираем образ фронтенда из папки frontend
                sh 'docker build -t transport-web-frontend ./frontend'
            }
        }

        stage('Test') {
            steps {
                echo 'Тестирование (можно добавить реальные тесты)...'
                // Пример: запуск тестов внутри контейнера перед поднятием сервиса
                // sh 'docker run --rm transport-web-backend pytest'
            }
        }

        stage('Deploy') {
            steps {
                echo 'Развертывание через docker-compose...'
                // Останавливаем и удаляем старые контейнеры (если есть)
                sh 'docker-compose down || true'
                // Запускаем новые контейнеры в фоне
                sh 'docker-compose up -d'
                // Проверяем статус
                sh 'docker-compose ps'
            }
        }
    }

    post {
        success {
            echo '✅ Пайплайн успешно выполнен!'
        }
        failure {
            echo '❌ Ошибка в пайплайне. Проверьте логи.'
        }
    }
}
